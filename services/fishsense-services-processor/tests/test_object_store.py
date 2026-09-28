# pylint: disable=protected-access
"""The processor's side of the object store (moto-backed).

Ported from fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/tests/test_object_store.py,
and the halves of libs/fishsense-shared/tests/test_object_store.py this side
depends on (path-style addressing, `_get` closing the StreamingBody, the model
key). v1's rules, kept:

* raw and slate scratch are read, processed JPEGs written; nothing is deleted
  -- the processor has no way to;
* weights come from the models bucket under ``models_prefix``, never scratch;
* a missing object raises ``NoSuchKey`` rather than reading as empty;
* every read closes the StreamingBody, or the connection pool drains.

v2 changes, each pinned here:

* **the processor reads and writes the ``ObjectRef`` it is handed** rather than
  building keys from a checksum: only the orchestrator issues keys (PLAN.md
  §9.11), so a key layout change cannot leave the two sides disagreeing;
* **it writes only a tenant's processed JPEG**: an upload anywhere but
  ``tenants/.../*.JPG`` in the labels bucket is refused, so a bad ref can never
  overwrite scratch, weights, or a JPEG v1 wrote that a Label Studio task still
  serves;
* **raw frames and weights are downloaded to a file**, streamed and renamed
  into place, instead of held in memory: a raw frame is ~15 MB and SAM 3.1's
  weights are gigabytes, and a failed download leaves no partial file behind.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_processor import object_store as sut

BUCKET = "fishsense-test"
LABELS = "labels-fishsense-test"
MODELS = "model-weights-test"
TENANT = "7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11"


def _settings(**overrides) -> ObjectStoreConnection:
    values = {
        "endpoint_url": "http://garage.example.com",
        "region": "garage",
        "access_key_id": "k",
        "secret_access_key": "s",
        "bucket": BUCKET,
        "labels_bucket": LABELS,
        "models_bucket": MODELS,
    }
    values.update(overrides)
    return ObjectStoreConnection(**values)


@pytest.fixture(name="s3")
def s3_fixture():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        for bucket in (BUCKET, LABELS, MODELS):
            client.create_bucket(Bucket=bucket)
        yield client


def _store(s3, **overrides) -> sut.ProcessorObjectStore:
    return sut.ProcessorObjectStore(s3, _settings(**overrides))


def _raw(checksum="deadbeef") -> ObjectRef:
    return ObjectRef(bucket=BUCKET, key=f"tenants/{TENANT}/raw/{checksum}.ORF")


def _jpeg(folder="preprocess_jpeg", checksum="cafef00d") -> ObjectRef:
    return ObjectRef(bucket=LABELS, key=f"tenants/{TENANT}/{folder}/{checksum}.JPG")


# -- the client ----------------------------------------------------------------


def test_the_client_uses_path_style_addressing_for_garage():
    """Garage has no virtual-host bucket DNS. A regression to virtual-host
    addressing would send every request to `bucket.garage.example.com`."""
    client = sut.build_s3_client(_settings())

    assert client.meta.config.s3["addressing_style"] == "path"
    assert client.meta.config.signature_version == "s3v4"
    assert client.meta.endpoint_url == "http://garage.example.com"
    assert client.meta.region_name == "garage"


# -- reads -----------------------------------------------------------------------


async def test_download_raw_writes_the_frame_to_a_file(s3, tmp_path):
    s3.put_object(Bucket=BUCKET, Key=_raw().key, Body=b"\x00\x01RAW")
    store = _store(s3)

    async def _run():
        return await store.download_raw(_raw(), tmp_path)

    path = await ActivityEnvironment().run(_run)

    assert path == tmp_path / "deadbeef.ORF"
    assert path.read_bytes() == b"\x00\x01RAW"


async def test_download_raw_streams_a_large_frame_intact(s3, tmp_path):
    payload = bytes(range(256)) * 20_000  # ~5 MB: many chunks
    s3.put_object(Bucket=BUCKET, Key=_raw().key, Body=payload)

    path = await _store(s3).download_raw(_raw(), tmp_path)

    assert path.read_bytes() == payload


async def test_download_raw_raises_on_missing_key_and_leaves_no_file(s3, tmp_path):
    """A frame the orchestrator never staged is an error, not an empty file --
    and nothing partial is left for a retry to mistake for the frame."""
    with pytest.raises(ClientError) as exc_info:
        await _store(s3).download_raw(_raw("missing"), tmp_path)

    assert exc_info.value.response["Error"]["Code"] == "NoSuchKey"
    assert list(tmp_path.iterdir()) == []


async def test_a_download_that_fails_midway_leaves_no_file_and_closes_the_body(
    tmp_path,
):
    closed: list[bool] = []

    class _Body:
        def iter_chunks(self, chunk_size):  # pylint: disable=unused-argument
            yield b"half a frame"
            raise ConnectionError("reset by peer")

        def close(self):
            closed.append(True)

    class _S3:
        def get_object(self, **_kwargs):
            return {"Body": _Body()}

    store = sut.ProcessorObjectStore(_S3(), _settings())
    with pytest.raises(ConnectionError):
        await store.download_raw(_raw(), tmp_path)

    assert list(tmp_path.iterdir()) == []
    assert closed == [True], "StreamingBody was not closed"


async def test_reading_bytes_closes_the_streaming_body():
    """botocore hands back a StreamingBody that holds an HTTP connection.
    Leaking it across repeated downloads exhausts the pool and stalls the
    activity (v1: the data-worker fixed this, the api-worker didn't)."""
    closed: list[bool] = []

    class _Body:
        def read(self):
            return b"PAYLOAD"

        def close(self):
            closed.append(True)

    class _S3:
        def get_object(self, **_kwargs):
            return {"Body": _Body()}

    store = sut.ProcessorObjectStore(_S3(), _settings())

    assert await store.download_processed_jpeg(_jpeg()) == b"PAYLOAD"
    assert closed == [True], "StreamingBody was not closed"


async def test_download_slate_pdf_returns_bytes(s3):
    ref = ObjectRef(bucket=BUCKET, key=f"tenants/{TENANT}/slate_pdf/5.pdf")
    s3.put_object(Bucket=BUCKET, Key=ref.key, Body=b"%PDF-1.7")
    store = _store(s3)

    async def _run():
        return await store.download_slate_pdf(ref)

    assert await ActivityEnvironment().run(_run) == b"%PDF-1.7"


async def test_download_processed_jpeg_reads_a_jpeg_v1_wrote(s3):
    """Head/tail predict reads the stage-5.1 JPEG, the exact frame the labeler
    is shown. For a frame v1 rendered, that JPEG is where v1 put it -- the
    orchestrator resolves it, and the processor reads the ref as given."""
    legacy = ObjectRef(
        bucket=LABELS, key="fishsense-lite/preprocess_headtail_jpeg/cafef00d.JPG"
    )
    s3.put_object(Bucket=LABELS, Key=legacy.key, Body=b"V1-JPEG")

    assert await _store(s3).download_processed_jpeg(legacy) == b"V1-JPEG"


# -- writes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder",
    [
        "preprocess_jpeg",
        "preprocess_groups_jpeg",
        "preprocess_headtail_jpeg",
        "preprocess_slate_images_jpeg",
        "checkerboard_lattice_jpeg",
    ],
)
async def test_upload_processed_jpeg_writes_the_ref_it_is_handed(s3, folder):
    store = _store(s3)
    ref = _jpeg(folder)

    async def _run():
        await store.upload_processed_jpeg(ref, b"JPEGBYTES")

    await ActivityEnvironment().run(_run)

    assert s3.get_object(Bucket=LABELS, Key=ref.key)["Body"].read() == b"JPEGBYTES"
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


async def test_split_buckets_read_scratch_write_labels(s3, tmp_path):
    s3.put_object(Bucket=BUCKET, Key=_raw().key, Body=b"RAW")
    store = _store(s3)

    raw = await store.download_raw(_raw(), tmp_path)
    await store.upload_processed_jpeg(_jpeg("preprocess_groups_jpeg"), b"JPG")

    assert raw.read_bytes() == b"RAW"
    key = _jpeg("preprocess_groups_jpeg").key
    assert s3.get_object(Bucket=LABELS, Key=key)["Body"].read() == b"JPG"
    with pytest.raises(ClientError):
        s3.get_object(Bucket=BUCKET, Key=key)


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param(
            ObjectRef(bucket=LABELS, key="fishsense-lite/preprocess_jpeg/c.JPG"),
            id="v1-jpeg-label-studio-serves",
        ),
        pytest.param(
            ObjectRef(bucket=BUCKET, key=f"tenants/{TENANT}/preprocess_jpeg/c.JPG"),
            id="scratch-bucket",
        ),
        pytest.param(
            ObjectRef(bucket=LABELS, key=f"tenants/{TENANT}/raw/c.ORF"),
            id="not-a-jpeg",
        ),
        pytest.param(
            ObjectRef(bucket=MODELS, key="sam3/3.1/sam3.1_multiplex.pt"),
            id="weights",
        ),
        pytest.param(ObjectRef(bucket=LABELS, key="tenants/c.JPG"), id="no-tenant"),
    ],
)
async def test_the_processor_writes_nothing_but_a_tenants_processed_jpeg(s3, ref):
    """The processor runs on infrastructure we don't own (PLAN.md §9.11), and a
    JPEG v1 wrote is what a Label Studio task shows a labeler: overwriting one
    silently changes the picture under an existing label."""
    with pytest.raises(sut.RefusedWrite):
        await _store(s3).upload_processed_jpeg(ref, b"JPG")

    for bucket in (BUCKET, LABELS, MODELS):
        assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


def test_the_processor_cannot_delete():
    """v1's asymmetry, kept: the orchestrator stages scratch in and deletes it
    after; the processor reads it and writes JPEGs, and has no delete at all."""
    assert not [name for name in dir(sut.ProcessorObjectStore) if "delete" in name]


# -- model weights ---------------------------------------------------------------


def test_model_key_carries_no_content_type_prefix():
    """Weights get their own bucket, so the key must not restate it."""
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
    assert sut.model_key("sam3", "3.1", "sam3.1_multiplex.pt", prefix) == expected


async def test_download_model_reads_from_the_models_bucket(s3, tmp_path):
    """Weights live in their own bucket, not the scratch one (v1: a checkpoint
    in a dedicated models bucket was a 404 at cold start)."""
    s3.put_object(Bucket=MODELS, Key="sam3/3.1/sam3.1_multiplex.pt", Body=b"WEIGHTS")
    s3.put_object(Bucket=BUCKET, Key="sam3/3.1/sam3.1_multiplex.pt", Body=b"WRONG")
    store = _store(s3)

    async def _run():
        return await store.download_model(
            "sam3", "3.1", "sam3.1_multiplex.pt", tmp_path
        )

    path = await ActivityEnvironment().run(_run)
    assert path.read_bytes() == b"WEIGHTS"


async def test_download_model_applies_the_models_prefix(s3, tmp_path):
    s3.put_object(
        Bucket=MODELS,
        Key="fishsense-lite/sam3/3.1/sam3.1_multiplex.pt",
        Body=b"WEIGHTS",
    )
    store = _store(s3, models_prefix="fishsense-lite")

    path = await store.download_model("sam3", "3.1", "sam3.1_multiplex.pt", tmp_path)

    assert path.read_bytes() == b"WEIGHTS"
