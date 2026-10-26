"""The shadow calibration's processor activities and workflows."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_contracts.shadow_calibration import (
    SegmentSlateFrame,
    SegmentSlateFramesInput,
    SlateSegment,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_processor.shadow_calibration.activities import (
    ShadowCalibrationActivities,
)
from fishsense_services_processor.shadow_calibration.workflow import (
    SegmentSlateFramesWorkflow,
)


def _jpeg(w=4014, h=3016):
    buf = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), 90, np.uint8)).save(buf, format="JPEG")
    return buf.getvalue()


class _Store:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.read = data, error, []

    async def download_image(self, ref=None, url=None):
        self.read.append(url)
        if self.error:
            raise self.error
        return self.data


def _acts(store, gpu=True):
    async def checkpoint():
        return Path("/w/sam3.1.pt"), "sam3/3.1@abc"

    def segmenter(path):
        def segment(image, prompt):
            m = np.zeros(image.shape[:2], bool)
            m[500:700, 700:900] = True
            return [(m, 0.7)] if prompt == "white board" else []

        return segment

    return ShadowCalibrationActivities(store_factory=lambda: store, sam3_checkpoint=checkpoint,
                                       cuda_available=lambda: gpu, segmenter=segmenter)  # fmt: skip


async def test_a_frame_is_read_by_url_and_segmented_under_its_dots():
    store = _Store(_jpeg())
    frame = SegmentSlateFrame(capture_id=uuid.uuid4(), render_url="https://g/r",
                              dots=[[800 * 4014 / 1600, 600 * 4014 / 1600], [1.0, 1.0]])  # fmt: skip
    r = await ActivityEnvironment().run(_acts(store).segment_slate_frame, frame)
    assert store.read == ["https://g/r"]
    assert (r.status, r.sam_model) == ("segmented", "sam3/3.1@abc")
    assert r.render_scale == pytest.approx(4014 / 1600)
    assert (r.dots[0].prompt, r.dots[0].mask_area_px) == ("white board", 200 * 200)
    assert r.dots[1].size is None


@pytest.mark.parametrize("store, status", [(_Store(error=RuntimeError("404")), "render_unavailable"),
                                           (_Store(b"not a jpeg"), "decode_failed")])  # fmt: skip
async def test_a_render_not_there_or_unreadable_is_the_frames_status(store, status):
    frame = SegmentSlateFrame(
        capture_id=uuid.uuid4(), render_url="u", dots=[[1.0, 1.0]]
    )
    r = await ActivityEnvironment().run(_acts(store).segment_slate_frame, frame)
    assert r.status == status and r.dots == []


async def test_a_worker_without_a_gpu_refuses():
    frame = SegmentSlateFrame(capture_id=uuid.uuid4(), render_url="u", dots=[])
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(
            _acts(_Store(_jpeg()), gpu=False).segment_slate_frame, frame
        )
    assert e.value.type == "NoGpuForSam3" and e.value.non_retryable


FAIL: set = set()


@activity.defn(name="segment_slate_frame")
async def _segment(frame: SegmentSlateFrame) -> SlateSegment:
    if frame.capture_id in FAIL:
        raise ApplicationError("boom")
    if str(frame.capture_id).startswith("11111111"):
        raise ApplicationError(
            "weights", type="Sam3WeightsUnavailable", non_retryable=True
        )
    if str(frame.capture_id).startswith("00000000"):
        raise ApplicationError("no GPU", type="NoGpuForSam3", non_retryable=True)
    return SlateSegment(capture_id=frame.capture_id, status="segmented")


async def _run(frames):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="q", workflows=[SegmentSlateFramesWorkflow],
                          activities=[_segment], workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                SegmentSlateFramesWorkflow.run,
                SegmentSlateFramesInput(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                                        frames=frames),
                id=f"t-{uuid.uuid4()}", task_queue="q",
            )  # fmt: skip


async def test_a_frame_that_fails_every_try_is_left_out():
    bad, good = uuid.uuid4(), uuid.uuid4()
    FAIL.add(bad)
    try:
        out = await _run([SegmentSlateFrame(capture_id=c, render_url="u", dots=[])
                          for c in (bad, good)])  # fmt: skip
    finally:
        FAIL.clear()
    assert [s.capture_id for s in out] == [good]


async def test_a_worker_that_cannot_run_sam_fails_the_child():
    with pytest.raises(WorkflowFailureError):
        await _run(
            [SegmentSlateFrame(capture_id=uuid.UUID(int=1), render_url="u", dots=[])]
        )


async def test_a_weights_failure_fails_the_child_rather_than_its_frames():
    """2026-10-24: weights that cannot load are the worker's, as no GPU is;
    charged to each frame, every frame of the mission was failed for it."""
    with pytest.raises(WorkflowFailureError) as e:
        await _run(
            [
                SegmentSlateFrame(capture_id=c, render_url="u", dots=[])
                for c in (
                    uuid.UUID("11111111-0000-0000-0000-000000000001"),
                    uuid.uuid4(),
                )
            ]
        )
    assert e.value.cause.cause.type == "Sam3WeightsUnavailable"
