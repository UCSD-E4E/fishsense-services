"""Calibration v4's GPU activity and workflow: a frame's render read by its
URL and the template's PDF once, a frame posed; a render not there is the
frame's status; no GPU fails the child; a frame failing every try is
recorded as failed."""

from __future__ import annotations

import uuid
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_contracts.calibration_v4 import (
    SlatePose,
    SlatePoseFrame,
    SlatePoseFramesInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_processor.slate_pose.activities import SlatePoseActivities
from fishsense_services_processor.slate_pose.workflow import SlatePoseFramesWorkflow

from . import test_calibration_v4_pose as synth
from .test_slate_pose_frame import _sam, _scene


def _pdf(page) -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=8.5 * 72, height=11 * 72)
    p.insert_image(p.rect, stream=cv2.imencode(".png", page)[1].tobytes())
    return doc.tobytes()


class _Store:
    def __init__(self, jpeg, pdf, error=None):
        self.jpeg, self.pdf, self.error = jpeg, pdf, error
        self.images, self.pdfs = [], []

    async def download_image(self, ref=None, url=None):
        self.images.append(url)
        if self.error:
            raise self.error
        return self.jpeg

    async def download_slate_pdf(self, ref):
        self.pdfs.append(ref.key)
        return self.pdf


def _acts(store, pose_, gpu=True):
    async def checkpoint():
        return Path("/w/sam3.1.pt"), "sam3/3.1@abc"

    return SlatePoseActivities(store_factory=lambda: store, sam3_checkpoint=checkpoint,
                               cuda_available=lambda: gpu,
                               segmenter=lambda path: _sam(pose_))  # fmt: skip


def _request(dot, refs, n=1):
    return SlatePoseFramesInput(
        tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(), camera_matrix=synth.K.tolist(),
        slate_pdf=ObjectRef(bucket="scratch", key="slate_pdf/t.pdf"), slate_dpi=synth.DPI,
        reference_points=refs.tolist(),
        frames=[SlatePoseFrame(capture_id=uuid.uuid4(), render_url=f"https://g/{i}",
                               dot=[float(dot[0]), float(dot[1])]) for i in range(n)],
    )  # fmt: skip


async def test_a_frame_is_posed_and_the_pdf_read_once():
    jpeg, dot, page, refs, target, pose_ = _scene()
    store = _Store(jpeg, _pdf(page))
    acts = _acts(store, pose_)
    req = _request(dot, refs, n=2)
    out = [await ActivityEnvironment().run(acts.slate_pose_frame,
                                           req.model_copy(update={"frames": [f]}))
           for f in req.frames]  # fmt: skip
    assert [o.status for o in out] == ["posed", "posed"]
    assert store.images == ["https://g/0", "https://g/1"] and len(store.pdfs) == 1
    assert out[0].capture_id == req.frames[0].capture_id
    assert np.linalg.norm(np.array(out[0].point) - target) < 0.005 * target[2]
    # The other way up's correlation and the page's up reach the payload (v23).
    assert out[0].ecc_other is not None and out[0].ecc_other < out[0].ecc
    assert out[0].page_up_deg is not None
    assert out[0].board_point is not None and len(out[0].board_point) == 3


async def test_a_render_not_there_is_the_frames_status():
    jpeg, dot, page, refs, _, pose_ = _scene()
    store = _Store(jpeg, _pdf(page), error=RuntimeError("404"))
    out = await ActivityEnvironment().run(_acts(store, pose_).slate_pose_frame,
                                          _request(dot, refs))  # fmt: skip
    assert out.status == "render_unavailable" and out.point is None


async def test_a_worker_without_a_gpu_refuses():
    jpeg, dot, page, refs, _, pose_ = _scene()
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(
            _acts(_Store(jpeg, _pdf(page)), pose_, gpu=False).slate_pose_frame,
            _request(dot, refs),
        )
    assert e.value.type == "NoGpuForSam3" and e.value.non_retryable


FAIL: set = set()


@activity.defn(name="slate_pose_frame")
async def _pose(req: SlatePoseFramesInput) -> SlatePose:
    (frame,) = req.frames
    if frame.capture_id in FAIL:
        raise ApplicationError("boom")
    if str(frame.capture_id).startswith("11111111"):
        raise ApplicationError("weights", type="Sam3WeightsCorrupt", non_retryable=True)
    if str(frame.capture_id).startswith("00000000"):
        raise ApplicationError("no GPU", type="NoGpuForSam3", non_retryable=True)
    return SlatePose(capture_id=frame.capture_id, status="posed")


async def _run(ids):
    req = _request([1.0, 1.0], np.zeros((1, 2)))
    req = req.model_copy(update={"frames": [SlatePoseFrame(capture_id=c, render_url="u",
                                                           dot=[1.0, 1.0]) for c in ids]})  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="q", workflows=[SlatePoseFramesWorkflow],
                          activities=[_pose], workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                SlatePoseFramesWorkflow.run, req, id=f"t-{uuid.uuid4()}", task_queue="q"
            )


async def test_each_frame_is_posed_and_one_failing_every_try_is_recorded():
    bad, good = uuid.uuid4(), uuid.uuid4()
    FAIL.add(bad)
    try:
        out = await _run([bad, good])
    finally:
        FAIL.clear()
    assert [(p.capture_id, p.status) for p in out] == [(bad, "failed"), (good, "posed")]


async def test_a_worker_that_cannot_run_sam_fails_the_child():
    with pytest.raises(WorkflowFailureError):
        await _run([uuid.UUID(int=1)])


async def test_a_weights_failure_fails_the_child_rather_than_its_frames():
    """2026-10-24: weights that cannot load are the worker's, as no GPU is;
    charged to each frame, every frame of the mission was failed for it."""
    with pytest.raises(WorkflowFailureError) as e:
        await _run([uuid.UUID("11111111-0000-0000-0000-000000000001"), uuid.uuid4()])
    assert e.value.cause.cause.type == "Sam3WeightsCorrupt"
