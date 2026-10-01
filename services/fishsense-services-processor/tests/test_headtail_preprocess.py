"""Stage 5.1 on the processor: rectify a raw frame, write its JPEG.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/: test_encode_rectified_jpeg.py,
test_stage5_1_notebook_parity.py, test_stage5_1_integration.py and
test_preprocess_headtail_images_workflow.py. Names, bodies and reasons are
v1's; the v2 adaptations:

* the activity is handed ObjectRefs -- the staged raw frame and where its JPEG
  goes -- rather than a checksum and a hard-coded `output_folder`: only the
  orchestrator issues keys (PLAN.md §9.11);
* v1's integration test ran against the devcontainer's Temporal and Garage;
  here the object store is moto, and the real `.ORF` goes through the real
  activity and the real `ProcessorObjectStore`;
* the intrinsics are fishsense-core's own `CameraIntrinsics` (v1: the API
  SDK's, which core no longer needs).
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import boto3
import cv2
import numpy as np
import pytest
from moto import mock_aws
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.headtail import (
    PreprocessHeadtailImage,
    PreprocessHeadtailImageInput,
    PreprocessHeadtailImagesInput,
)
from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_processor.headtail_preprocess import activities as sut
from fishsense_services_processor.headtail_preprocess.workflow import (
    PreprocessHeadtailImagesWorkflow,
)
from fishsense_services_processor.object_store import ProcessorObjectStore

_FIXTURE = Path(__file__).parent / "fixtures" / "stage2_sample.ORF"
_K = [[3000.0, 0.0, 2000.0], [0.0, 3000.0, 1500.0], [0.0, 0.0, 1.0]]
_D = [-0.05, 0.01, 0.0, 0.0, 0.0]
TENANT = uuid.uuid4()
SCRATCH, LABELS = "fishsense-test", "labels-fishsense-test"


def _raw(checksum="abc"):
    return ObjectRef(bucket=SCRATCH, key=f"tenants/{TENANT}/raw/{checksum}.ORF")


def _jpeg(checksum="abc"):
    return ObjectRef(
        bucket=LABELS, key=f"tenants/{TENANT}/preprocess_headtail_jpeg/{checksum}.JPG"
    )


@pytest.fixture
def orf_path() -> Path:
    if not _FIXTURE.exists():
        pytest.skip(f"missing fixture {_FIXTURE}")
    return _FIXTURE


# -- encoding (v1's test_encode_rectified_jpeg.py) ---------------------------------


def test_returns_valid_jpeg_bytes():
    img = np.full((1500, 2000, 3), fill_value=128, dtype=np.uint8)
    out = sut.encode_rectified_jpeg(img)
    assert out[:2] == b"\xff\xd8"
    assert len(out) > 1024


def test_decoded_jpeg_keeps_input_shape():
    img = np.full((1000, 1500, 3), fill_value=64, dtype=np.uint8)
    out = sut.encode_rectified_jpeg(img)
    decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (1000, 1500, 3)


def test_does_not_mutate_input():
    img = np.full((400, 600, 3), fill_value=200, dtype=np.uint8)
    original = img.copy()
    sut.encode_rectified_jpeg(img)
    assert np.array_equal(img, original)


# -- notebook parity (v1's test_stage5_1_notebook_parity.py) ------------------------


def test_worker_transform_matches_notebook_byte_for_byte(orf_path: Path):
    """The notebook did `RawImage(path).data` -> `cv2.undistort` ->
    `cv2.imwrite('.JPG')`; the stage must stay byte-identical to it -- no
    overlay, default JPEG quality."""
    from fishsense_core.image.raw_image import RawImage

    img = RawImage(orf_path).data
    img = cv2.undistort(img, np.array(_K), np.array(_D))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.JPG"
        cv2.imwrite(out.as_posix(), img)
        notebook = out.read_bytes()

    worker = sut.rectify_and_encode_jpeg(orf_path.read_bytes(), _K, _D)

    assert notebook == worker, "stage 5.1 worker and notebook diverge"


# -- the activity -------------------------------------------------------------------


class _FakeStore:
    def __init__(self, raw_bytes=b"raw"):
        self.raw_bytes = raw_bytes
        self.downloaded: list[ObjectRef] = []
        self.uploaded: list[tuple[ObjectRef, bytes]] = []

    async def download_raw(self, ref, directory):
        self.downloaded.append(ref)
        path = Path(directory) / ref.key.rsplit("/", 1)[-1]
        path.write_bytes(self.raw_bytes)
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.uploaded.append((ref, data))


async def test_the_activity_renders_the_frame_it_is_handed_where_it_is_told(
    monkeypatch,
):
    """v2: the raw ref and the JPEG ref come from the orchestrator; the
    processor builds no key."""
    rendered = []

    def fake_render(raw_bytes, camera_matrix, distortion_coefficients):
        rendered.append((raw_bytes, camera_matrix, distortion_coefficients))
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(sut, "rectify_and_encode_jpeg", fake_render)
    store = _FakeStore(raw_bytes=b"orf bytes")
    activities = sut.HeadtailPreprocessActivities(store_factory=lambda: store)

    await ActivityEnvironment().run(
        activities.preprocess_headtail_image,
        PreprocessHeadtailImageInput(
            raw=_raw(), jpeg=_jpeg(), camera_matrix=_K, distortion_coefficients=_D
        ),
    )

    assert store.downloaded == [_raw()]
    assert rendered == [(b"orf bytes", _K, _D)]
    assert store.uploaded == [(_jpeg(), b"\xff\xd8jpeg")]


async def test_the_store_is_built_once_and_only_when_first_needed(monkeypatch):
    """Settings are read on first use (not at import, which the role registry
    does in every test), then the one client is reused."""
    monkeypatch.setattr(sut, "rectify_and_encode_jpeg", lambda *a: b"j")
    built = []

    def factory():
        built.append(1)
        return _FakeStore()

    activities = sut.HeadtailPreprocessActivities(store_factory=factory)
    assert not built
    payload = PreprocessHeadtailImageInput(
        raw=_raw(), jpeg=_jpeg(), camera_matrix=_K, distortion_coefficients=_D
    )
    for _ in range(2):
        await ActivityEnvironment().run(activities.preprocess_headtail_image, payload)

    assert built == [1]


async def test_workflow_processes_one_image_end_to_end(orf_path: Path, monkeypatch):
    """v1's test_stage5_1_integration.py, on moto rather than the
    devcontainer: the real frame through the real activity and store."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (SCRATCH, LABELS):
            s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket=SCRATCH, Key=_raw().key, Body=orf_path.read_bytes())
        settings = ObjectStoreConnection(
            endpoint_url="http://garage.example.com", region="us-east-1",
            access_key_id="k", secret_access_key="s", bucket=SCRATCH,
            labels_bucket=LABELS, legacy_labels_prefix="fishsense-lite",
        )  # fmt: skip
        store = ProcessorObjectStore(s3, settings)
        activities = sut.HeadtailPreprocessActivities(store_factory=lambda: store)

        await ActivityEnvironment().run(
            activities.preprocess_headtail_image,
            PreprocessHeadtailImageInput(
                raw=_raw(), jpeg=_jpeg(), camera_matrix=_K, distortion_coefficients=_D
            ),
        )

        content = s3.get_object(Bucket=LABELS, Key=_jpeg().key)["Body"].read()

    assert content[:2] == b"\xff\xd8"
    decoded = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[0] >= 1000 and decoded.shape[1] >= 1000


# -- the workflow (v1's test_preprocess_headtail_images_workflow.py) ----------------


def _payload(checksums):
    return PreprocessHeadtailImagesInput(
        tenant_id=TENANT,
        dive_id=uuid.uuid4(),
        images=[
            PreprocessHeadtailImage(
                capture_id=uuid.uuid4(), checksum=c, raw=_raw(c), jpeg=_jpeg(c)
            )
            for c in checksums
        ],
        camera_matrix=_K,
        distortion_coefficients=_D,
    )


async def _run_workflow(payload, stub, queue):
    from temporalio.contrib.pydantic import pydantic_data_converter

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[PreprocessHeadtailImagesWorkflow],
            activities=[stub],
        ):
            await env.client.execute_workflow(
                PreprocessHeadtailImagesWorkflow.run,
                payload,
                id=f"{queue}-{uuid.uuid4()}",
                task_queue=queue,
            )


async def test_workflow_fans_out_one_activity_per_image_with_correct_args():
    calls: List[PreprocessHeadtailImageInput] = []

    @activity.defn(name="preprocess_headtail_image")
    async def stub(payload: PreprocessHeadtailImageInput) -> None:
        calls.append(payload)

    await _run_workflow(_payload(["a", "b", "c"]), stub, "test-stage51")

    assert {c.raw for c in calls} == {_raw("a"), _raw("b"), _raw("c")}
    assert {c.jpeg for c in calls} == {_jpeg("a"), _jpeg("b"), _jpeg("c")}
    for c in calls:
        assert c.camera_matrix == _K
        assert c.distortion_coefficients == _D


async def test_workflow_uses_start_to_close_not_schedule_to_close():
    """Per-image activities are timed by execution, not queue+execution: with
    a fan-out on a small pool, schedule_to_close ticks down while activities
    wait for a slot -- the dive-76 head/tail run failed exactly this way."""
    timeouts: List[Tuple[Optional[timedelta], Optional[timedelta]]] = []

    @activity.defn(name="preprocess_headtail_image")
    async def stub(payload: PreprocessHeadtailImageInput) -> None:  # noqa: ARG001
        info = activity.info()
        timeouts.append((info.start_to_close_timeout, info.schedule_to_close_timeout))

    await _run_workflow(_payload(["a"]), stub, "test-stage51-timeouts")

    ((start_to_close, schedule_to_close),) = timeouts
    assert start_to_close == timedelta(minutes=5), "v1's per-image budget"
    if schedule_to_close is not None and schedule_to_close > timedelta(0):
        assert schedule_to_close > start_to_close


async def test_workflow_with_no_images_makes_no_activity_calls():
    calls: List[PreprocessHeadtailImageInput] = []

    @activity.defn(name="preprocess_headtail_image")
    async def stub(payload: PreprocessHeadtailImageInput) -> None:
        calls.append(payload)

    await _run_workflow(_payload([]), stub, "test-stage51-empty")

    assert not calls
