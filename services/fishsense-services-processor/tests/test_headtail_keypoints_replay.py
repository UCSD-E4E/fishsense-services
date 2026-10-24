"""The SimCC keypoint replay on the GPU role: a frame's render read by URL,
the model on the decoded render and on its crop JPEG-encoded again (the
round-trip variant lifted back to frame pixels), refusals and unreadable
renders as the frame's status, no GPU failing the child, a frame failing
every try recorded as failed. A stand-in detector: the real network is
fishsense-core's, tested there."""

from __future__ import annotations

import uuid

import cv2
import numpy as np
import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_contracts.headtail_keypoints import (
    KeypointFrame,
    KeypointFramesInput,
    KeypointResult,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_processor.headtail_keypoints.activities import (
    HeadtailKeypointActivities,
)
from fishsense_services_processor.headtail_keypoints.predict import keypoints_frame
from fishsense_services_processor.headtail_keypoints.workflow import (
    PredictHeadtailKeypointsWorkflow,
)


class _Detector:
    """Snout at the crop's centre-left, fork at centre-right (crop pixels);
    records what it was given."""

    def __init__(self):
        self.frames, self.crops, self.pixels = [], [], []

    def predict(self, frame, mask_bbox):
        from fishsense_core.fish.headtail_keypoints import (  # pylint: disable=import-outside-toplevel
            HeadTailKeypoints,
            crop_from_box,
        )

        self.frames.append(frame.shape)
        x0, y0, x1, y1 = crop_from_box(mask_bbox, frame.shape[1], frame.shape[0])
        if x1 <= x0 or y1 <= y0:
            raise ValueError("mask_bbox lies outside the frame")
        (h, t), c = self._points(y1 - y0, x1 - x0)
        return HeadTailKeypoints(head=(h[0] + x0, h[1] + y0), tail=(t[0] + x0, t[1] + y0),
                                 confidence=c, crop=(x0, y0, x1, y1))  # fmt: skip

    def predict_crops(self, crops):
        self.crops.append([c.shape for c in crops])
        self.pixels.append([c.copy() for c in crops])
        out = [self._points(*c.shape[:2]) for c in crops]
        return np.array([p for p, _ in out]), np.array([c for _, c in out])

    @staticmethod
    def _points(h, w):
        return ((0.25 * w, 0.5 * h), (0.75 * w, 0.5 * h)), 0.9


def _jpeg(w=1200, h=900):
    img = np.full((h, w, 3), 90, np.uint8)
    cv2.rectangle(img, (400, 300), (700, 450), (200, 180, 160), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


def test_both_inputs_give_points_in_frame_pixels():
    det = _Detector()
    out = keypoints_frame(_jpeg(), [400, 300, 700, 450], det)
    assert out["status"] == "predicted"
    x0, y0, x1, y1 = out["crop"]
    assert out["head"] == pytest.approx([x0 + 0.25 * (x1 - x0), y0 + 0.5 * (y1 - y0)])
    # the round trip is the same crop, re-encoded, lifted by the same origin
    assert out["rt_head"] == pytest.approx(out["head"])
    assert out["rt_tail"] == pytest.approx(out["tail"])
    assert det.frames == [(900, 1200, 3)] and det.crops == [[(y1 - y0, x1 - x0, 3)]]


def test_the_round_trip_crop_is_jpeg_encoded_again(monkeypatch):
    jpeg = _jpeg()
    seen = []
    real = cv2.imencode

    def spy(ext, img, *a):
        seen.append((ext, img.shape, a))
        return real(ext, img, *a)

    monkeypatch.setattr(cv2, "imencode", spy)
    out = keypoints_frame(jpeg, [400, 300, 700, 450], _Detector())
    x0, y0, x1, y1 = out["crop"]
    assert seen == [(".jpg", (y1 - y0, x1 - x0, 3), ([cv2.IMWRITE_JPEG_QUALITY, 95],))]


def test_the_model_is_given_the_re_encoded_crop_not_the_frames_pixels():
    jpeg, det = _jpeg(), _Detector()
    out = keypoints_frame(jpeg, [400, 300, 700, 450], det)
    x0, y0, x1, y1 = out["crop"]
    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    raw = frame[y0:y1, x0:x1]
    expected = cv2.imdecode(cv2.imencode(".jpg", raw, [cv2.IMWRITE_JPEG_QUALITY, 95])[1],
                            cv2.IMREAD_COLOR)  # fmt: skip
    (given,) = det.pixels[0]
    assert np.array_equal(given, expected) and not np.array_equal(given, raw)


def test_a_box_the_model_refuses_is_the_frames_status():
    out = keypoints_frame(_jpeg(), [5000, 5000, 5100, 5100], _Detector())
    assert out["status"] == "refused" and out.get("head") is None


def test_an_unreadable_render_raises():
    with pytest.raises(ValueError):
        keypoints_frame(b"not a jpeg", [1, 1, 5, 5], _Detector())


class _Store:
    def __init__(self, data, error=None):
        self.data, self.error, self.read = data, error, []

    async def download_image(self, ref=None, url=None):
        self.read.append(url)
        if self.error:
            raise self.error
        return self.data


def _acts(store, gpu=True):
    return HeadtailKeypointActivities(store_factory=lambda: store,
                                      detector=lambda: _Detector(),
                                      checkpoint="headtail-keypoints/simcc-sam-e12",
                                      cuda_available=lambda: gpu)  # fmt: skip


def _frame(**kw):
    return KeypointFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                         render_url=kw.get("url", "https://g/r"),
                         mask_bbox=kw.get("box", [400, 300, 700, 450]))  # fmt: skip


@pytest.mark.parametrize("store, status", [(_Store(None, RuntimeError("404")), "render_unavailable"),
                                           (_Store(b"not a jpeg"), "decode_failed")])  # fmt: skip
async def test_a_render_not_there_or_unreadable_is_the_frames_status(store, status):
    f = _frame()
    r = await ActivityEnvironment().run(_acts(store).predict_headtail_keypoints, f)
    assert (r.status, r.prediction_id, r.head) == (status, f.prediction_id, None)


async def test_a_frame_is_read_by_url_and_keypointed():
    store = _Store(_jpeg())
    f = _frame()
    r = await ActivityEnvironment().run(_acts(store).predict_headtail_keypoints, f)
    assert store.read == ["https://g/r"] and r.status == "predicted"
    assert r.checkpoint == "headtail-keypoints/simcc-sam-e12" and r.rt_head is not None


async def test_a_worker_without_a_gpu_refuses():
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(
            _acts(_Store(_jpeg()), gpu=False).predict_headtail_keypoints, _frame()
        )
    assert e.value.type == "NoGpuForKeypoints" and e.value.non_retryable


FAIL: set = set()


@activity.defn(name="predict_headtail_keypoints")
async def _predict(frame: KeypointFrame) -> KeypointResult:
    if frame.capture_id in FAIL:
        raise ApplicationError("boom")
    if str(frame.capture_id).startswith("11111111"):
        raise ApplicationError(
            "weights", type="HeadtailKeypointsWeightsCorrupt", non_retryable=True
        )
    if str(frame.capture_id).startswith("00000000"):
        raise ApplicationError("no GPU", type="NoGpuForKeypoints", non_retryable=True)
    return KeypointResult(prediction_id=frame.prediction_id, capture_id=frame.capture_id,
                          status="predicted")  # fmt: skip


async def _run(ids):
    frames = [KeypointFrame(prediction_id=uuid.uuid4(), capture_id=c, render_url="u",
                            mask_bbox=[1, 1, 5, 5]) for c in ids]  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="q",
                          workflows=[PredictHeadtailKeypointsWorkflow], activities=[_predict],
                          workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                PredictHeadtailKeypointsWorkflow.run,
                KeypointFramesInput(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                                    frames=frames),
                id=f"t-{uuid.uuid4()}", task_queue="q",
            )  # fmt: skip


async def test_each_frame_is_keypointed_and_one_failing_every_try_is_recorded():
    bad, good = uuid.uuid4(), uuid.uuid4()
    FAIL.add(bad)
    try:
        out = await _run([bad, good])
    finally:
        FAIL.clear()
    assert [(r.capture_id, r.status) for r in out] == [
        (bad, "failed"),
        (good, "predicted"),
    ]


async def test_a_worker_that_cannot_run_the_model_fails_the_child():
    with pytest.raises(WorkflowFailureError):
        await _run([uuid.UUID(int=1)])


async def test_a_weights_failure_fails_the_child_rather_than_its_frames():
    """2026-10-24: weights that cannot load are the worker's, as no GPU is;
    charged to each frame, every frame of the mission was failed for it."""
    with pytest.raises(WorkflowFailureError) as e:
        await _run([uuid.UUID("11111111-0000-0000-0000-000000000001"), uuid.uuid4()])
    assert e.value.cause.cause.type == "HeadtailKeypointsWeightsCorrupt"
