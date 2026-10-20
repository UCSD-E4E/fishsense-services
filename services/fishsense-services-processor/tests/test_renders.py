"""The render stage: a capture's raw rectified to a JPEG, PUT to its tenant's
bucket through a presigned URL, its size and MD5 reported (2026-10-19)."""

from __future__ import annotations

import hashlib
import io
import uuid
from pathlib import Path
from typing import List

import pytest
import rawpy
from botocore.exceptions import ClientError
from PIL import Image
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.renders import (
    RENDER_VERSION,
    RenderCaptureInput,
    RenderCapturesInput,
    RenderFrame,
    RenderResult,
    render_key,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_processor import registry
from fishsense_services_processor.renders.activities import RenderActivities
from fishsense_services_processor.renders.workflow import RenderCapturesWorkflow

CAMERA = uuid.UUID(int=21)
K = [[2850.0, 0.0, 2000.0], [0.0, 2850.0, 1500.0], [0.0, 0.0, 1.0]]
SUM = "c" * 32


def _jpeg(w=40, h=30) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(out, "JPEG")
    return out.getvalue()


class _Store:
    def __init__(self, *, missing=False):
        self.missing = missing

    async def download_raw(self, ref, directory: Path) -> Path:
        if self.missing:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        path = directory / "frame.ORF"
        path.write_bytes(b"raw")
        return path


class _Put:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, url, headers, data) -> int:
        self.calls.append((url, headers, data))
        return self.status


def _activities(store=None, put=None, render=None):
    return RenderActivities(
        store_factory=lambda: store or _Store(),
        render_jpeg=render or (lambda raw, camera, dist: _jpeg()),
        put=put or _Put(),
    )


def _payload(version=RENDER_VERSION) -> RenderCaptureInput:
    return RenderCaptureInput(
        frame=RenderFrame(capture_id=uuid.uuid4(),
                          raw=ObjectRef(bucket="scratch", key=f"raw/{SUM}.ORF"),
                          key=render_key(RENDER_VERSION, CAMERA, SUM),
                          put_url="https://garage.example/lab/renders/x?sig=1"),
        render_version=version, camera_calibration_id=CAMERA,
        camera_matrix=K, distortion_coefficients=[0.0] * 5,
    )  # fmt: skip


async def _run(acts, payload):
    return await ActivityEnvironment().run(acts.render_capture, payload)


async def test_a_capture_is_rendered_and_put_where_the_orchestrator_presigned():
    put = _Put()
    payload = _payload()

    r = await _run(_activities(put=put), payload)

    ((url, headers, data),) = put.calls
    assert url == payload.frame.put_url and data == _jpeg()
    assert headers == {"Content-Type": "image/jpeg"}
    assert (r.status, r.bytes, r.md5) == (
        "rendered",
        len(_jpeg()),
        hashlib.md5(_jpeg()).hexdigest(),
    )
    assert (r.width, r.height) == (40, 30)
    assert (r.render_version, r.camera_calibration_id) == (RENDER_VERSION, CAMERA)


async def test_a_raw_missing_from_scratch_is_an_abstention():
    put = _Put()
    r = await _run(_activities(store=_Store(missing=True), put=put), _payload())
    assert r.status == "raw_unavailable" and put.calls == []


async def test_a_raw_that_will_not_decode_is_an_abstention():
    def corrupt(*args):
        raise rawpy.LibRawError("Input data is corrupted")

    put = _Put()
    r = await _run(_activities(put=put, render=corrupt), _payload())
    assert r.status == "decode_failed" and put.calls == []


async def test_a_refused_put_fails_the_try():
    with pytest.raises(RuntimeError, match="403"):
        await _run(_activities(put=_Put(403)), _payload())


async def test_another_render_version_is_refused_and_retried():
    """A worker at another version (a lab worker deployed by hand) must not
    write its rendering under another version's key."""
    put = _Put()
    with pytest.raises(ApplicationError) as error:
        await _run(_activities(put=put), _payload(RENDER_VERSION + 1))
    assert error.value.type == "RenderVersionMismatch"
    assert not error.value.non_retryable and put.calls == []


async def test_the_workflow_renders_each_capture_and_leaves_out_one_that_keeps_failing():
    from temporalio import activity

    bad = uuid.uuid4()

    @activity.defn(name="render_capture")
    async def stub(payload: RenderCaptureInput) -> RenderResult:
        if payload.frame.capture_id == bad:
            raise RuntimeError("PUT 500")
        assert payload.camera_calibration_id == CAMERA
        return RenderResult(capture_id=payload.frame.capture_id, status="rendered",
                            render_version=payload.render_version,
                            camera_calibration_id=CAMERA, bytes=1, md5="0" * 32)  # fmt: skip

    frames = [
        RenderFrame(capture_id=c, raw=ObjectRef(bucket="s", key="r"),
                    key="renders/k.JPG", put_url="https://x")  # fmt: skip
        for c in (uuid.uuid4(), bad, uuid.uuid4())
    ]
    payload = RenderCapturesInput(
        tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(), render_version=RENDER_VERSION,
        camera_calibration_id=CAMERA, camera_matrix=K,
        distortion_coefficients=[0.0] * 5, frames=frames,
    )  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-render", activities=[stub],
                          workflows=[RenderCapturesWorkflow]):  # fmt: skip
            results = await env.client.execute_workflow(
                RenderCapturesWorkflow.run, payload,
                id=f"t-render-{uuid.uuid4()}", task_queue="t-render",
                result_type=List[RenderResult],
            )  # fmt: skip

    assert [r.capture_id for r in results] == [frames[0].capture_id,
                                               frames[2].capture_id]  # fmt: skip


def test_the_render_stage_runs_on_the_per_image_role():
    """Decoding a raw needs the per-image role's memory, not a GPU."""
    (stage,) = [s for s in registry.stages() if s.name == "renders"]
    assert stage.role == registry.ROLE_PER_IMAGE
