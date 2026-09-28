"""Stage 0.1 on the processor: rectify a raw frame, draw the expected-laser
region, write the JPEG where the orchestrator said.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/ (test_preprocess_laser_images_workflow.py,
test_stage0_1_notebook_parity.py, test_stage0_1_integration.py). v1's reasons,
kept: one activity per image with a start-to-close bound (a dive's images
queue behind the per-image cap of 2), and the bbox path byte-identical to the
notebook. v2 changes, pinned here:

* each image is an `ObjectRef` in and an `ObjectRef` out -- the processor never
  builds a key, and writes only a processed JPEG (`ProcessorObjectStore`);
* the intrinsics are fishsense-core's own `CameraIntrinsics` (core #83), not
  the v1 API SDK's;
* the real-frame tests read the frame from ``FISHSENSE_TEST_ORF`` (v1's
  15 MB `stage2_sample.ORF` is not committed here) and skip without it.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import List

import boto3
import cv2
import numpy as np
import pytest
from moto import mock_aws
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.laser import (
    LaserPreprocessImage,
    PreprocessLaserImagesInput,
)
from fishsense_services_contracts.laser_region import (
    DEFAULT_LASER_BBOX,
    LASER_REGION_POLYGON,
)
from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_processor.laser_preprocess import activities as sut
from fishsense_services_processor.laser_preprocess.workflow import (
    PreprocessLaserImageInput,
    PreprocessLaserImagesWorkflow,
)
from fishsense_services_processor.object_store import ProcessorObjectStore

TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")
_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
_D = [-0.1, 0.05, 0.0, 0.0, 0.0]
_BBOX = [1800, 700, 2400, 1600]


def _raw(checksum):
    return ObjectRef(bucket="scratch", key=f"tenants/{TENANT}/raw/{checksum}.ORF")


def _jpeg(checksum):
    return ObjectRef(
        bucket="labels", key=f"tenants/{TENANT}/preprocess_jpeg/{checksum}.JPG"
    )


def _images(*checksums):
    return [
        LaserPreprocessImage(
            capture_id=uuid.uuid5(uuid.NAMESPACE_OID, c), raw=_raw(c), jpeg=_jpeg(c)
        )
        for c in checksums
    ]


async def _run_workflow(payload, stub):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-stage01",
            workflows=[PreprocessLaserImagesWorkflow],
            activities=[stub],
        ):
            await env.client.execute_workflow(
                PreprocessLaserImagesWorkflow.run,
                payload,
                id=f"test-stage01-{uuid.uuid4()}",
                task_queue="test-stage01",
            )


def _payload(images, **overrides):
    values = {
        "dive_id": uuid.uuid4(),
        "images": images,
        "camera_matrix": _K,
        "distortion_coefficients": _D,
        "bbox": _BBOX,
    }
    values.update(overrides)
    return PreprocessLaserImagesInput(**values)


async def test_workflow_fans_out_one_activity_per_image_with_correct_args():
    calls: List[PreprocessLaserImageInput] = []

    @activity.defn(name="preprocess_laser_image")
    async def stub(payload: PreprocessLaserImageInput) -> None:
        calls.append(payload)

    images = _images("a", "b", "c")
    await _run_workflow(_payload(images, laser_region=LASER_REGION_POLYGON), stub)

    assert {c.raw for c in calls} == {i.raw for i in images}
    by_raw = {c.raw: c for c in calls}
    for image in images:
        call = by_raw[image.raw]
        assert call.jpeg == image.jpeg
        assert list(call.bbox) == _BBOX
        assert call.region == LASER_REGION_POLYGON
        assert call.camera_matrix == _K
        assert call.distortion_coefficients == _D


async def test_workflow_uses_start_to_close_not_schedule_to_close():
    """Queue wait > schedule_to_close on dives with image counts far above the
    per-image pool's size was a prod failure (v1)."""
    timeouts = []

    @activity.defn(name="preprocess_laser_image")
    async def stub(payload: PreprocessLaserImageInput) -> None:  # noqa: ARG001
        info = activity.info()
        timeouts.append((info.start_to_close_timeout, info.schedule_to_close_timeout))

    await _run_workflow(_payload(_images("a")), stub)

    start_to_close, schedule_to_close = timeouts[0]
    assert start_to_close == timedelta(minutes=5)
    if schedule_to_close:
        assert schedule_to_close > start_to_close


async def test_workflow_with_no_images_makes_no_activity_calls():
    calls: list = []

    @activity.defn(name="preprocess_laser_image")
    async def stub(payload: PreprocessLaserImageInput) -> None:
        calls.append(payload)

    await _run_workflow(_payload([]), stub)

    assert not calls


# --- the activity --------------------------------------------------------------


class _Store:
    def __init__(self):
        self.uploads: dict = {}

    async def download_raw(self, ref, directory):
        path = Path(directory) / ref.key.rsplit("/", 1)[-1]
        path.write_bytes(b"ORF:" + ref.key.encode())
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.uploads[ref] = data


def _activity_payload(**overrides):
    values = {
        "capture_id": uuid.uuid4(),
        "raw": _raw("abc"),
        "jpeg": _jpeg("abc"),
        "bbox": DEFAULT_LASER_BBOX,
        "camera_matrix": _K,
        "distortion_coefficients": _D,
        "region": LASER_REGION_POLYGON,
    }
    values.update(overrides)
    return PreprocessLaserImageInput(**values)


async def test_the_activity_writes_the_jpeg_where_it_was_told(monkeypatch):
    store = _Store()
    monkeypatch.setattr(sut, "_object_store", lambda: store)
    seen = {}

    def _fake(raw_path, camera_matrix, distortion_coefficients, bbox, region):
        seen.update(raw=Path(raw_path).read_bytes(), bbox=bbox, region=region)
        return b"\xff\xd8JPEG"

    monkeypatch.setattr(sut, "_rectify_overlay_encode", _fake)

    await ActivityEnvironment().run(sut.preprocess_laser_image, _activity_payload())

    assert store.uploads == {_jpeg("abc"): b"\xff\xd8JPEG"}
    assert seen["raw"] == b"ORF:" + _raw("abc").key.encode()
    assert seen["region"] == LASER_REGION_POLYGON
    assert tuple(seen["bbox"]) == tuple(DEFAULT_LASER_BBOX)


async def test_a_migrated_frame_is_redrawn_over_v1s_jpeg(monkeypatch):
    """The orchestrator hands a migrated frame v1's key (a redraw overwrites
    in place, so Label Studio's task URL never moves); the processor's store
    accepts it, and writes nothing that is not a processed JPEG."""
    settings = ObjectStoreConnection(
        endpoint_url="https://s3.test",
        region="garage",
        access_key_id="k",
        secret_access_key="s",
        bucket="scratch",
        labels_bucket="labels",
        legacy_labels_prefix="fishsense-lite",
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="scratch")
        s3.create_bucket(Bucket="labels")
        s3.put_object(Bucket="scratch", Key=_raw("abc").key, Body=b"raw")
        store = ProcessorObjectStore(s3, settings)
        monkeypatch.setattr(sut, "_object_store", lambda: store)
        monkeypatch.setattr(sut, "_rectify_overlay_encode", lambda *a: b"\xff\xd8new")
        v1_jpeg = ObjectRef(
            bucket="labels", key="fishsense-lite/preprocess_jpeg/abc.JPG"
        )

        await ActivityEnvironment().run(
            sut.preprocess_laser_image, _activity_payload(jpeg=v1_jpeg)
        )

        body = s3.get_object(Bucket="labels", Key=v1_jpeg.key)["Body"].read()
    assert body == b"\xff\xd8new"


# --- a real frame -------------------------------------------------------------

_ORF = os.environ.get("FISHSENSE_TEST_ORF")
_REAL_K = [[3000.0, 0.0, 2000.0], [0.0, 3000.0, 1500.0], [0.0, 0.0, 1.0]]
_REAL_D = [-0.05, 0.01, 0.0, 0.0, 0.0]
_NOTEBOOK_BBOX = (1800, 700, 2400, 1600)

needs_orf = pytest.mark.skipif(
    not (_ORF and Path(_ORF).exists()),
    reason="set FISHSENSE_TEST_ORF to a real TG-6 .ORF (v1's stage2_sample.ORF)",
)


@needs_orf
def test_worker_transform_matches_notebook_byte_for_byte():
    """The bbox path is what the notebook drew: byte parity, v1's test."""
    from fishsense_core.camera_intrinsics import CameraIntrinsics
    from fishsense_core.image.raw_image import RawImage
    from fishsense_core.image.rectified_image import RectifiedImage

    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(_REAL_K), distortion_coefficients=np.array(_REAL_D)
    )
    img = RectifiedImage(RawImage(Path(_ORF)), intrinsics).data
    img = cv2.rectangle(img, _NOTEBOOK_BBOX[:2], _NOTEBOOK_BBOX[2:], (0, 255, 0), 2)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.JPG"
        cv2.imwrite(out.as_posix(), img)
        notebook = out.read_bytes()

    worker = sut._rectify_overlay_encode(  # pylint: disable=protected-access
        Path(_ORF), _REAL_K, _REAL_D, _NOTEBOOK_BBOX, None
    )

    assert worker == notebook, "stage 0.1 worker and notebook diverge"


@needs_orf
async def test_a_real_frame_becomes_a_valid_jpeg_through_the_object_store(
    monkeypatch,
):
    """v1's stage-0.1 integration test, on moto rather than the devcontainer's
    Garage: the real decode, the polygon, the upload."""
    settings = ObjectStoreConnection(
        endpoint_url="https://s3.test",
        region="garage",
        access_key_id="k",
        secret_access_key="s",
        bucket="scratch",
        labels_bucket="labels",
        legacy_labels_prefix="fishsense-lite",
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="scratch")
        s3.create_bucket(Bucket="labels")
        s3.put_object(
            Bucket="scratch", Key=_raw("real").key, Body=Path(_ORF).read_bytes()
        )
        monkeypatch.setattr(
            sut, "_object_store", lambda: ProcessorObjectStore(s3, settings)
        )

        await ActivityEnvironment().run(
            sut.preprocess_laser_image,
            _activity_payload(
                raw=_raw("real"),
                jpeg=_jpeg("real"),
                camera_matrix=_REAL_K,
                distortion_coefficients=_REAL_D,
            ),
        )
        body = s3.get_object(Bucket="labels", Key=_jpeg("real").key)["Body"].read()

    decoded = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.shape[2] == 3
