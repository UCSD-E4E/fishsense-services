# pylint: disable=protected-access
"""The orchestrator's object-store client (moto-backed).

Ported from fishsense-lite@77e8f8e5:
services/fishsense-api-workflow-worker/tests/test_object_store.py (the
api-worker's staging and cleanup vocabulary) and
libs/fishsense-shared/tests/test_object_store.py (the S3 primitives and the
client's addressing). v1's rules, kept:

* staging HEAD-checks, then PUTs; cleanup DELETEs, idempotently;
* **only a not-found is "absent"**: a 403 or 500 propagates, or staging would
  re-upload on every firing and cleanup would think it had nothing to delete;
* every GET closes its StreamingBody, or botocore's pool drains;
* raw staging targets scratch, and the JPEG check the labels bucket;
* Garage wants path-style addressing and SigV4.

v2 changes, each pinned here: keys are the tenant's (see
test_object_store_layout.py), and the JPEG check is the legacy key resolver's
-- it answers *where* the JPEG is (new key first, then v1's for a migrated
frame), not only whether.
"""

from __future__ import annotations

import uuid

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_orchestrator.object_store import store as sut
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

BUCKET = "fishsense-test"
LABELS = "labels-fishsense-test"
TENANT = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
SLATE = uuid.UUID("5b0f1f5e-9a3c-4d8e-8e1d-1a2b3c4d5e6f")
ABC = "abc123"


def _settings(**overrides) -> ObjectStoreConnection:
    values = {
        "endpoint_url": "http://garage.example.com",
        "region": "garage",
        "access_key_id": "k",
        "secret_access_key": "s",
        "bucket": BUCKET,
        "labels_bucket": LABELS,
        "legacy_labels_prefix": "fishsense-lite",
    }
    values.update(overrides)
    return ObjectStoreConnection(**values)


@pytest.fixture(name="s3")
def s3_fixture():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.create_bucket(Bucket=LABELS)
        yield client


def _store(s3, **overrides) -> sut.OrchestratorObjectStore:
    return sut.OrchestratorObjectStore(s3, ObjectLayout(_settings(**overrides)))


def _keys(s3, bucket=BUCKET) -> set[str]:
    return {o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])}


def _body(s3, key, bucket=BUCKET) -> bytes:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


RAW_KEY = f"tenants/{TENANT}/raw/{ABC}.ORF"


# -- the client ----------------------------------------------------------------


def test_the_client_uses_path_style_addressing_for_garage():
    client = sut.build_s3_client(_settings())

    assert client.meta.config.s3["addressing_style"] == "path"
    assert client.meta.config.signature_version == "s3v4"
    assert client.meta.endpoint_url == "http://garage.example.com"
    assert client.meta.region_name == "garage"


def test_from_settings_builds_the_layout_from_the_same_settings():
    store = sut.OrchestratorObjectStore.from_settings(_settings())

    assert store.layout.raw(TENANT, ABC) == ObjectRef(bucket=BUCKET, key=RAW_KEY)


# -- primitives ------------------------------------------------------------------


async def test_exists_true_when_object_present(s3):
    s3.put_object(Bucket=BUCKET, Key="k", Body=b"x")

    assert await _store(s3)._exists(ObjectRef(bucket=BUCKET, key="k")) is True


async def test_exists_false_when_object_missing(s3):
    assert await _store(s3)._exists(ObjectRef(bucket=BUCKET, key="nope")) is False


async def test_exists_checks_the_refs_bucket(s3):
    s3.put_object(Bucket=LABELS, Key="k", Body=b"j")
    store = _store(s3)

    assert await store._exists(ObjectRef(bucket=LABELS, key="k")) is True
    assert await store._exists(ObjectRef(bucket=BUCKET, key="k")) is False


async def test_exists_reraises_non_not_found_errors():
    """A 403/500 must not be silently reported as "absent" -- that would make
    staging re-upload on every firing, or make cleanup believe it had nothing
    to delete."""

    class _Boom:
        def head_object(self, **_kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "HeadObject"
            )

    store = sut.OrchestratorObjectStore(_Boom(), ObjectLayout(_settings()))
    with pytest.raises(ClientError) as exc_info:
        await store._exists(ObjectRef(bucket=BUCKET, key="k"))
    assert exc_info.value.response["Error"]["Code"] == "AccessDenied"


async def test_get_returns_bytes_and_closes_the_streaming_body():
    closed: list[bool] = []

    class _Body:
        def read(self):
            return b"PAYLOAD"

        def close(self):
            closed.append(True)

    class _S3:
        def get_object(self, **_kwargs):
            return {"Body": _Body()}

    store = sut.OrchestratorObjectStore(_S3(), ObjectLayout(_settings()))

    assert await store._get(ObjectRef(bucket=BUCKET, key="k")) == b"PAYLOAD"
    assert closed == [True], "StreamingBody was not closed"


async def test_get_raises_on_missing_key(s3):
    with pytest.raises(ClientError) as exc_info:
        await _store(s3)._get(ObjectRef(bucket=BUCKET, key="missing"))
    assert exc_info.value.response["Error"]["Code"] == "NoSuchKey"


async def test_delete_removes_the_object_and_is_idempotent(s3):
    s3.put_object(Bucket=BUCKET, Key="k", Body=b"x")
    store = _store(s3)

    await store._delete(ObjectRef(bucket=BUCKET, key="k"))
    assert _keys(s3) == set()
    # S3 delete_object on an absent key succeeds, so retries are safe.
    await store._delete(ObjectRef(bucket=BUCKET, key="k"))


# -- raw staging and cleanup -----------------------------------------------------


async def test_upload_raw_writes_expected_key_and_bytes(s3):
    store = _store(s3)

    async def _run():
        await store.upload_raw(TENANT, ABC, b"raw-bytes")

    await ActivityEnvironment().run(_run)

    assert _keys(s3) == {RAW_KEY}
    assert _body(s3, RAW_KEY) == b"raw-bytes"


async def test_has_raw_reflects_presence(s3):
    store = _store(s3)

    assert await store.has_raw(TENANT, "nope") is False
    await store.upload_raw(TENANT, "yep", b"x")
    assert await store.has_raw(TENANT, "yep") is True


async def test_raw_is_the_tenants_own(s3):
    """Another tenant's staged copy of the same frame is not this tenant's."""
    store = _store(s3)
    await store.upload_raw(uuid.uuid4(), ABC, b"x")

    assert await store.has_raw(TENANT, ABC) is False


async def test_delete_raw_removes_scratch_object_and_is_idempotent(s3):
    store = _store(s3)
    await store.upload_raw(TENANT, "gone", b"x")

    assert await store.delete_raw(TENANT, "gone") is True
    assert await store.delete_raw(TENANT, "gone") is True
    assert _keys(s3) == set()


async def test_staging_uses_scratch_bucket_even_with_labels_configured(s3):
    store = _store(s3)

    await store.upload_raw(TENANT, ABC, b"RAW")

    assert await store.has_raw(TENANT, ABC) is True
    assert _body(s3, RAW_KEY) == b"RAW"
    assert _keys(s3, LABELS) == set()


async def test_upload_slate_pdf_writes_expected_key(s3):
    store = _store(s3)

    await store.upload_slate_pdf(TENANT, SLATE, b"%PDF-1.7")

    assert await store.has_slate_pdf(TENANT, SLATE) is True
    key = f"tenants/{TENANT}/slate_pdf/{SLATE}.pdf"
    assert _keys(s3) == {key}
    assert await store.download_slate_pdf(TENANT, SLATE) == b"%PDF-1.7"


# -- the processed-JPEG check ----------------------------------------------------

NEW_JPEG = f"tenants/{TENANT}/preprocess_groups_jpeg/caf.JPG"
V1_JPEG = "fishsense-lite/preprocess_groups_jpeg/caf.JPG"


async def test_has_processed_jpeg_checks_labels_bucket_and_tenant_prefix(s3):
    store = _store(s3)

    async def _check():
        return await store.has_processed_jpeg(
            TENANT, "preprocess_groups_jpeg", "caf", from_v1=False
        )

    assert await ActivityEnvironment().run(_check) is False
    # Written into the scratch bucket: does NOT count.
    s3.put_object(Bucket=BUCKET, Key=NEW_JPEG, Body=b"x")
    assert await ActivityEnvironment().run(_check) is False
    # Only the labels bucket at the tenant's key counts.
    s3.put_object(Bucket=LABELS, Key=NEW_JPEG, Body=b"x")
    assert await ActivityEnvironment().run(_check) is True


async def test_a_jpeg_v1_wrote_is_found_for_a_frame_migrated_from_v1(s3):
    s3.put_object(Bucket=LABELS, Key=V1_JPEG, Body=b"v1")

    located = await _store(s3).locate_processed_jpeg(
        TENANT, "preprocess_groups_jpeg", "caf", from_v1=True
    )

    assert located == ObjectRef(bucket=LABELS, key=V1_JPEG)


async def test_a_jpeg_v2_rendered_wins_over_v1s(s3):
    s3.put_object(Bucket=LABELS, Key=V1_JPEG, Body=b"v1")
    s3.put_object(Bucket=LABELS, Key=NEW_JPEG, Body=b"v2")

    located = await _store(s3).locate_processed_jpeg(
        TENANT, "preprocess_groups_jpeg", "caf", from_v1=True
    )

    assert located == ObjectRef(bucket=LABELS, key=NEW_JPEG)


async def test_a_v1_jpeg_never_answers_for_a_frame_v2_ingested(s3):
    """v1's keys carry no tenant: a partner's frame that happens to share a
    checksum with a lab frame must not be told the lab's JPEG is its own."""
    s3.put_object(Bucket=LABELS, Key=V1_JPEG, Body=b"v1")
    store = _store(s3)

    assert (
        await store.locate_processed_jpeg(
            TENANT, "preprocess_groups_jpeg", "caf", from_v1=False
        )
        is None
    )
    assert (
        await store.has_processed_jpeg(
            TENANT, "preprocess_groups_jpeg", "caf", from_v1=False
        )
        is False
    )


def test_the_orchestrator_never_writes_a_processed_jpeg():
    """The processor writes JPEGs; the orchestrator only checks for them (v1's
    api-worker/data-worker split)."""
    assert not [
        name
        for name in dir(sut.OrchestratorObjectStore)
        if "jpeg" in name and not name.startswith(("has_", "locate_"))
    ]
