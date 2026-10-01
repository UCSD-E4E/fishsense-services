"""The object store's shared half: how both sides reach Garage, and the
reference the orchestrator hands the processor.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
test_object_store.py (the settings-to-client half: `open_client`'s bucket
defaults and prefix normalisation). v1 read a Dynaconf ``[object_store]``
section and normalised it when a client was built; v2 reads
``FISHSENSE_OBJECT_STORE_*`` and normalises it once, at startup. v1's rules,
kept:

* ``labels_bucket`` falls back to ``bucket``, so a single-bucket deployment
  works unchanged;
* a prefix left empty (or None) is ``""``, never the string ``"None"`` in a key,
  and its surrounding slashes are stripped -- S3 reads ``a//b`` as a different
  object from ``a/b``.

v2 changes, each pinned here:

* **validated at startup**: a missing endpoint or an endpoint that isn't an
  http(s) URL fails the worker's start, not its first staging;
* the secret is a ``SecretStr``, so it never prints;
* v1's ``labels_prefix`` is ``legacy_labels_prefix``: new JPEGs are keyed under
  ``tenants/{tenant_id}/`` (PLAN.md §9.11) and the prefix only says where v1's
  JPEGs already are;
* the processor is handed an ``ObjectRef`` (bucket and key) rather than a
  checksum to build a key from: only the orchestrator issues keys (§9.11).
"""

import secrets

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.object_store import (
    TENANTS_PREFIX,
    ObjectRef,
    ObjectStoreConnection,
)

# Generated per run, never written in the source: a secret-shaped literal trips
# secret scanners even when it is fake.
SECRET = secrets.token_hex(16)

REQUIRED = {
    "FISHSENSE_OBJECT_STORE_ENDPOINT_URL": "https://s3.e4e.example",
    "FISHSENSE_OBJECT_STORE_REGION": "garage",
    "FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID": "GKexample",
    "FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY": SECRET,
    "FISHSENSE_OBJECT_STORE_BUCKET": "fishsense-lite",
    # Required: unset, every migrated frame's JPEG would read as "not written".
    "FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX": "fishsense-lite",
}


@pytest.fixture
def env(monkeypatch):
    for name in list(REQUIRED) + [
        "FISHSENSE_OBJECT_STORE_LABELS_BUCKET",
    ]:
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


# -- settings ------------------------------------------------------------------


def test_the_connection_is_read_from_fishsense_object_store_env(env):
    settings = ObjectStoreConnection()

    assert settings.endpoint_url == "https://s3.e4e.example"
    assert settings.region == "garage"
    assert settings.access_key_id == "GKexample"
    assert settings.secret_access_key.get_secret_value() == SECRET
    assert settings.bucket == "fishsense-lite"


def test_the_secret_never_prints(env):
    settings = ObjectStoreConnection()

    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.model_dump())


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_every_required_setting_fails_startup_when_missing(env, missing):
    env.delenv(missing)

    with pytest.raises(ValidationError):
        ObjectStoreConnection()


@pytest.mark.parametrize(
    "endpoint", ["s3.e4e.example", "ftp://s3.e4e.example", "https://", ""]
)
def test_an_endpoint_that_is_not_an_http_url_fails_startup(env, endpoint):
    env.setenv("FISHSENSE_OBJECT_STORE_ENDPOINT_URL", endpoint)

    with pytest.raises(ValidationError, match="endpoint_url"):
        ObjectStoreConnection()


def test_a_docker_hostname_without_a_tld_is_a_valid_endpoint(env):
    """v1's `url_condition` over `validators.url`: compose names hosts `garage`."""
    env.setenv("FISHSENSE_OBJECT_STORE_ENDPOINT_URL", "http://garage:3900/")

    assert ObjectStoreConnection().endpoint_url == "http://garage:3900"


def test_the_labels_bucket_defaults_to_the_scratch_bucket(env):
    """A single-bucket deployment doesn't set it (v1's `open_client`)."""
    assert ObjectStoreConnection().labels_bucket == "fishsense-lite"


def test_a_single_bucket_setup_says_so_with_an_empty_legacy_prefix(env):
    env.setenv("FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX", "")

    assert ObjectStoreConnection().legacy_labels_prefix == ""


def test_v1s_production_buckets_are_honoured(env):
    env.setenv("FISHSENSE_OBJECT_STORE_LABELS_BUCKET", "labels-fishsense-lite")
    settings = ObjectStoreConnection()

    assert settings.labels_bucket == "labels-fishsense-lite"
    assert settings.legacy_labels_prefix == "fishsense-lite"


@pytest.mark.parametrize("field", ["legacy_labels_prefix"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [("", ""), ("/fishsense-lite/", "fishsense-lite"), ("a/b/", "a/b")],
)
def test_prefixes_are_normalised_so_no_key_has_a_double_slash(
    env, field, value, expected
):
    env.setenv(f"FISHSENSE_OBJECT_STORE_{field.upper()}", value)

    assert getattr(ObjectStoreConnection(), field) == expected


def test_a_none_prefix_is_empty_not_the_string_none():
    """v1: `labels_prefix = ""` round-tripped as None on some Dynaconf paths."""
    settings = ObjectStoreConnection(
        endpoint_url="http://garage:3900",
        region="garage",
        access_key_id="k",
        secret_access_key=SECRET,
        bucket="b",
        legacy_labels_prefix=None,
    )

    assert settings.legacy_labels_prefix == ""


def test_the_legacy_prefix_cannot_be_the_tenant_namespace(env):
    """v1's JPEGs and v2's must never share a namespace: a legacy prefix of
    `tenants` would let a v1 key be read as a tenant's, or a tenant key as v1's."""
    env.setenv("FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX", TENANTS_PREFIX)

    with pytest.raises(ValidationError, match="legacy_labels_prefix"):
        ObjectStoreConnection()


# -- ObjectRef -----------------------------------------------------------------


def test_an_object_ref_round_trips_as_a_contract_payload():
    ref = ObjectRef(bucket="fishsense-lite", key="tenants/t/raw/abc.ORF")

    assert ObjectRef.model_validate_json(ref.model_dump_json()) == ref
    assert ref.uri == "s3://fishsense-lite/tenants/t/raw/abc.ORF"


@pytest.mark.parametrize(
    "key", ["", "/tenants/t/raw/abc.ORF", "tenants//raw/abc.ORF", "tenants/t/"]
)
def test_a_key_s3_would_read_as_a_different_object_is_refused(key):
    """A leading, doubled or trailing slash names a *different* object in S3, so
    a ref carrying one would read nothing, or write where nothing reads."""
    with pytest.raises(ValidationError):
        ObjectRef(bucket="fishsense-lite", key=key)


def test_an_object_ref_needs_a_bucket():
    with pytest.raises(ValidationError):
        ObjectRef(bucket="", key="tenants/t/raw/abc.ORF")


def test_an_object_ref_is_immutable_and_hashable():
    ref = ObjectRef(bucket="b", key="k")

    with pytest.raises(ValidationError):
        ref.key = "other"
    assert {ref: 1}[ObjectRef(bucket="b", key="k")] == 1
