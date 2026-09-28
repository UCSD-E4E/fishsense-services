"""Model weights from Garage's `model-weights` bucket, through core's WeightStore.

Ported from fishsense-lite@77e8f8e5: the weights half of
libs/fishsense-shared/tests/test_object_store.py (`model_key`, the models
bucket and prefix), the `download_model` tests of the data-worker's
tests/test_object_store.py, and tests/test_checkpoint_cache.py. v1 fetched a
checkpoint from Garage and cached it; v2 (PLAN.md §9.12) fetches through
`fishsense_core.models.fetch` with a `GarageWeightStore`, so what loads is also
gated by the manifest's sha256.

v2 changes, each pinned here:

* **nothing unverified loads.** v1 trusted whatever bytes the key held; a
  tampered or truncated object is now refused, and nothing is cached;
* a key the bucket does not hold is `ModelUnavailable` (core's "this store
  lacks it"), while any other S3 error propagates, as v1's `_exists` did;
* the weights have their own settings (`FISHSENSE_MODEL_WEIGHTS_*`), validated
  when the processor starts, and the cache directory is required: v1 defaulted
  it to a path only its PVC had.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from fishsense_core.models import (
    Manifest,
    ModelIntegrityError,
    ModelUnavailable,
    builtin_manifest,
)
from moto import mock_aws
from pydantic import SecretStr, ValidationError

from fishsense_services_processor import weights as sut

MODELS_BUCKET = "model-weights-test"
WEIGHTS = b"pretend these are gigabytes of SAM 3.1"


def _manifest(entries: dict[tuple[str, str, str], bytes]) -> Manifest:
    """A manifest pinning each `(name, version, filename)` to these bytes."""
    toml = ""
    for (name, version, filename), data in entries.items():
        toml += f"""
[[model]]
name = "{name}"
version = "{version}"

[[model.artifact]]
targets = ["server"]
filename = "{filename}"
sha256 = "{hashlib.sha256(data).hexdigest()}"
size = {len(data)}
"""
    return Manifest.parse(toml)


MANIFEST = _manifest({("sam3", "3.1", "sam3.1_multiplex.pt"): WEIGHTS})


@pytest.fixture(name="s3")
def s3_fixture(monkeypatch):
    # moto intercepts the calls; the credentials only have to exist.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=MODELS_BUCKET)
        yield client


class _Counting:
    """A store that counts how often `fetch` had to go to it."""

    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.threads = set()

    def download_to(self, ref, dest):
        self.calls += 1
        self.threads.add(threading.get_ident())
        self.store.download_to(ref, dest)


def _fetch(store, cache, name="sam3", version="3.1", manifest=MANIFEST):
    return sut.fetch_weights(
        name, version, store=store, cache_dir=cache, manifest=manifest
    )


# --------------------------------------------------------------------
# The key: v1's `model_key`
# --------------------------------------------------------------------


def test_model_key_carries_no_content_type_prefix():
    """Weights get their own bucket, so the key must not restate it.

    `raw/` and `slate_pdf/` are content-type prefixes because they share the
    one scratch bucket and must not collide. A dedicated models bucket makes
    a `models/` prefix pure repetition, so there is no MODEL_PREFIX to spell.
    """
    assert (
        sut.model_key("sam3", "3.1", "sam3.1_multiplex.pt")
        == "sam3/3.1/sam3.1_multiplex.pt"
    )
    assert not hasattr(sut, "MODEL_PREFIX")


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("", "sam3/3.1/sam3.1_multiplex.pt"),
        (None, "sam3/3.1/sam3.1_multiplex.pt"),
        ("fishsense-lite", "fishsense-lite/sam3/3.1/sam3.1_multiplex.pt"),
        ("/fishsense-lite/", "fishsense-lite/sam3/3.1/sam3.1_multiplex.pt"),
    ],
)
def test_model_key_prefix_handling(prefix, expected):
    """Same slash-stripping contract as `jpeg_key`: a `models_prefix` is only
    needed to partition a models bucket shared with another tenant, and a
    stray slash would name a different object."""
    assert sut.model_key("sam3", "3.1", "sam3.1_multiplex.pt", prefix) == expected


def test_the_key_is_the_cache_layout_core_fetches_into():
    """Garage's `{name}/{version}/{filename}` is core's cache layout too, so a
    bucket can be mirrored to a directory and served by `LocalDirStore`."""
    ref = MANIFEST.resolve("sam3", "3.1")
    assert sut.model_key(ref.name, ref.version, ref.filename) == (
        "sam3/3.1/sam3.1_multiplex.pt"
    )


# --------------------------------------------------------------------
# The store: v1's `download_model`, now behind core's WeightStore
# --------------------------------------------------------------------


def test_fetch_reads_from_the_models_bucket(s3, tmp_path):
    s3.put_object(
        Bucket=MODELS_BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=WEIGHTS
    )

    path = _fetch(sut.GarageWeightStore(s3, MODELS_BUCKET), tmp_path)

    assert path.read_bytes() == WEIGHTS
    assert path == tmp_path / "sam3" / "3.1" / "sam3.1_multiplex.pt"


def test_fetch_applies_the_models_prefix(s3, tmp_path):
    """A models bucket shared with another tenant is partitioned by prefix,
    the way `labels_prefix` partitions the labels bucket."""
    s3.put_object(
        Bucket=MODELS_BUCKET,
        Key="fishsense-lite/sam3/3.1/sam3.1_multiplex.pt",
        Body=WEIGHTS,
    )
    store = sut.GarageWeightStore(s3, MODELS_BUCKET, prefix="fishsense-lite")

    assert _fetch(store, tmp_path).read_bytes() == WEIGHTS


def test_a_fetch_is_verified_against_the_manifest(s3, tmp_path):
    """The weights that load are the ones the manifest pins, byte for byte.

    `fetch` hashes what the store wrote before it moves it into the cache, and
    stamps the cache with the sha256 it checked.
    """
    s3.put_object(
        Bucket=MODELS_BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=WEIGHTS
    )

    path = _fetch(sut.GarageWeightStore(s3, MODELS_BUCKET), tmp_path)

    ref = MANIFEST.resolve("sam3", "3.1")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ref.sha256
    stamp = path.with_name(path.name + ".sha256")
    assert stamp.read_text().strip() == ref.sha256


@pytest.mark.parametrize(
    "tampered",
    [
        WEIGHTS[:-1] + b"!",  # same size, different bytes
        WEIGHTS[:10],  # truncated upload
    ],
)
def test_a_tampered_object_is_refused(s3, tmp_path, tampered):
    """v2: v1 loaded whatever bytes the key held. Anyone who can write the
    bucket (or a half-finished upload) could have swapped the checkpoint under
    a version that every measurement then credits. Now the fetch refuses it,
    and nothing reaches the cache to be read as a hit later."""
    s3.put_object(
        Bucket=MODELS_BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=tampered
    )

    with pytest.raises(ModelIntegrityError):
        _fetch(sut.GarageWeightStore(s3, MODELS_BUCKET), tmp_path)

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_the_builtin_manifest_gates_what_loads(s3, tmp_path):
    """With no manifest override, core's own pins decide: a Garage object that
    is not the pinned laser detector is refused, whatever its key says."""
    ref = builtin_manifest().resolve("laser-detector", None)
    s3.put_object(
        Bucket=MODELS_BUCKET,
        Key=sut.model_key(ref.name, ref.version, ref.filename),
        Body=b"not the laser detector",
    )

    with pytest.raises(ModelIntegrityError):
        sut.fetch_weights(
            "laser-detector",
            store=sut.GarageWeightStore(s3, MODELS_BUCKET),
            cache_dir=tmp_path,
        )


def test_sam3_is_not_in_core_4_1_0s_manifest(s3, tmp_path):
    """SAM 3.1's hash is not pinned in fishsense-core 4.1.0, so the head/tail
    stage cannot fetch it through the built-in manifest yet. This fails the day
    core pins it, which is the day to delete the stage's workaround."""
    with pytest.raises(KeyError, match="sam3"):
        sut.fetch_weights(
            "sam3",
            "3.1",
            store=sut.GarageWeightStore(s3, MODELS_BUCKET),
            cache_dir=tmp_path,
        )


def test_a_missing_object_is_unavailable(s3, tmp_path):
    """Core's "not cached, and the store lacks it"."""
    with pytest.raises(ModelUnavailable, match="sam3/3.1/sam3.1_multiplex.pt"):
        _fetch(sut.GarageWeightStore(s3, MODELS_BUCKET), tmp_path)

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_other_s3_errors_are_not_reported_as_missing(tmp_path):
    """A 403 or a 500 is not "the bucket lacks it". Reporting it as unavailable
    would send an operator to re-upload weights that are there, when the grant
    is what is wrong."""

    class _Forbidden:
        def download_file(self, Bucket, Key, Filename):  # noqa: N803 (boto3's)
            raise ClientError(
                {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
            )

    with pytest.raises(ClientError):
        _fetch(sut.GarageWeightStore(_Forbidden(), MODELS_BUCKET), tmp_path)


# --------------------------------------------------------------------
# The cache: v1's checkpoint_cache guarantees, kept by core's fetch
# --------------------------------------------------------------------


def test_second_call_uses_the_cache(s3, tmp_path):
    """The processor is stood up per wake, so a cold start is the common case;
    within a pod, the second load must not re-download."""
    s3.put_object(
        Bucket=MODELS_BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=WEIGHTS
    )
    store = _Counting(sut.GarageWeightStore(s3, MODELS_BUCKET))

    _fetch(store, tmp_path)
    _fetch(store, tmp_path)

    assert store.calls == 1


def test_version_is_part_of_the_cache_path(s3, tmp_path):
    """New weights are a new object *and* a new cached file.

    If the version were not in the path, a bumped checkpoint would silently
    read the old bytes off the volume forever — the failure a cache keyed only
    on filename invites.
    """
    manifest = _manifest(
        {("sam3", "v1", "sam3.pt"): b"v1-bytes", ("sam3", "v2", "sam3.pt"): b"v2-bytes"}
    )
    s3.put_object(Bucket=MODELS_BUCKET, Key="sam3/v1/sam3.pt", Body=b"v1-bytes")
    s3.put_object(Bucket=MODELS_BUCKET, Key="sam3/v2/sam3.pt", Body=b"v2-bytes")
    store = sut.GarageWeightStore(s3, MODELS_BUCKET)

    p1 = _fetch(store, tmp_path, version="v1", manifest=manifest)
    p2 = _fetch(store, tmp_path, version="v2", manifest=manifest)

    assert p1 != p2
    assert p1.read_bytes() == b"v1-bytes"
    assert p2.read_bytes() == b"v2-bytes"


def test_partial_download_is_not_left_behind(tmp_path):
    """A failed download must not leave a truncated file that later reads as
    a valid cache hit."""

    class _Reset:
        def download_file(self, Bucket, Key, Filename):  # noqa: N803 (boto3's)
            Path(Filename).write_bytes(WEIGHTS[:5])
            raise ConnectionResetError("connection reset")

    with pytest.raises(ConnectionResetError):
        _fetch(sut.GarageWeightStore(_Reset(), MODELS_BUCKET), tmp_path)

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_concurrent_callers_download_once(s3, tmp_path):
    """Activities run in a real ThreadPoolExecutor, so a cold pod enters this
    with the whole first batch at once. Unguarded, each would download its own
    copy of a multi-gigabyte file."""
    s3.put_object(
        Bucket=MODELS_BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=WEIGHTS
    )
    store = _Counting(sut.GarageWeightStore(s3, MODELS_BUCKET))

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: _fetch(store, tmp_path), range(8)))

    assert len(set(paths)) == 1
    assert store.calls == 1


async def test_async_callers_download_once_without_blocking_the_loop(s3, tmp_path):
    """v1's `ensure_checkpoint` was a coroutine; an async activity awaits
    `afetch_weights`, which hashes gigabytes off the event loop (inline, it
    would starve the Temporal heartbeats that keep the activity alive)."""
    s3.put_object(
        Bucket=MODELS_BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=WEIGHTS
    )
    store = _Counting(sut.GarageWeightStore(s3, MODELS_BUCKET))

    paths = await asyncio.gather(
        *(
            sut.afetch_weights(
                "sam3", "3.1", store=store, cache_dir=tmp_path, manifest=MANIFEST
            )
            for _ in range(8)
        )
    )

    assert len(set(paths)) == 1
    assert store.calls == 1
    assert threading.get_ident() not in store.threads


# --------------------------------------------------------------------
# Settings: FISHSENSE_MODEL_WEIGHTS_*, validated at startup
# --------------------------------------------------------------------

REQUIRED_ENV = {
    "FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL": "https://s3.e4e.ucsd.edu",
    "FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID": "GK-model-reader",
    "FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY": "shh",
    "FISHSENSE_MODEL_WEIGHTS_CACHE_DIR": "/cache/models",
}


@pytest.fixture(name="env")
def env_fixture(monkeypatch):
    for key in list(REQUIRED_ENV) + [
        "FISHSENSE_MODEL_WEIGHTS_BUCKET",
        "FISHSENSE_MODEL_WEIGHTS_PREFIX",
        "FISHSENSE_MODEL_WEIGHTS_REGION",
    ]:
        monkeypatch.delenv(key, raising=False)

    def set_env(**overrides):
        for key, value in {**REQUIRED_ENV, **overrides}.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

    return set_env


def test_settings_default_to_v1s_production_bucket(env):
    env()

    settings = sut.ModelWeightsSettings()

    assert settings.bucket == "model-weights"
    assert settings.prefix == ""
    assert settings.region == "garage"
    assert settings.cache_dir == Path("/cache/models")
    assert isinstance(settings.secret_access_key, SecretStr)
    assert "shh" not in repr(settings)


@pytest.mark.parametrize("missing", sorted(REQUIRED_ENV))
def test_a_missing_setting_fails_at_startup(env, missing):
    """A processor that cannot reach its weights should not start and take
    work it will fail on its first model load. The cache directory is required
    too: it must be the volume the pod mounts, and v1's default named a PVC
    v2's pods do not have."""
    env(**{missing: None})

    with pytest.raises(ValidationError):
        sut.ModelWeightsSettings()


def test_the_endpoint_must_be_a_url(env):
    env(FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL="s3.e4e.ucsd.edu")

    with pytest.raises(ValidationError):
        sut.ModelWeightsSettings()


def test_the_prefix_is_stripped_of_slashes(env):
    env(FISHSENSE_MODEL_WEIGHTS_PREFIX="/fishsense-lite/")

    assert sut.ModelWeightsSettings().prefix == "fishsense-lite"


def test_the_store_speaks_garages_dialect(env):
    """Garage has no virtual-host bucket DNS, so addressing must be path-style,
    against an explicit endpoint and region, signed SigV4 (v1's
    `build_s3_client`)."""
    env()

    store = sut.GarageWeightStore.from_settings(sut.ModelWeightsSettings())

    client = store.s3
    assert client.meta.endpoint_url == "https://s3.e4e.ucsd.edu"
    assert client.meta.region_name == "garage"
    assert client.meta.config.s3["addressing_style"] == "path"
    assert client.meta.config.signature_version == "s3v4"
    assert store.bucket == "model-weights"
