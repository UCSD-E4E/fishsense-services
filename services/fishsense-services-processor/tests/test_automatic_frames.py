"""The automatic-results GPU stage: the dot, the SAM 3.1 mask at it, the head/tail.

New in v2. The chain is cscw-fishsense2027@96a8da07 e2e_measurement/run_e2e.py
(the laser pass, then `predict_from_jpeg` seeded by the *automatic* dot), run
here in one activity per frame, with stub models (no GPU, no torch):

* the dot is the production detector's, through the laser stage's own
  kernel and region gate; no dot is an abstention (`no_laser_dot`);
* the mask is the head/tail stage's kernel (crop around the dot, the dot must
  be on the mask, first hit wins), with **SAM's score >= 0.5** (paper §6.3) as
  the gate, recorded on the row;
* a **slate frame** (the slate-presence detector's p >= 0.5) keeps its dot for
  the label-free calibration and is never segmented as a fish;
* the rectified JPEG is written where the orchestrator says (it is the
  head/tail stage's rendering), so the species and calibration steps can read
  it; a raw frame missing from scratch is an abstention, not a retry loop;
* SAM 3.1 only: no Mask R-CNN fallback (its lengths were never validated), so
  a GPU-less worker refuses, non-retryably.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import List

import cv2
import numpy as np
import pytest
import rawpy
from botocore.exceptions import ClientError
from fishsense_services_contracts.temporal import DATA_CONVERTER
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AUTOMATIC_JPEG_RENDER_VERSION,
    AutomaticFrame,
    AutomaticFrameResult,
    PredictAutomaticFrameInput,
    PredictAutomaticFramesInput,
)
from fishsense_services_contracts.object_store import ObjectRef, automatic_jpeg_folder
import hashlib

from fishsense_services_contracts.masks import MaskUpload, mask_key
from fishsense_services_processor.automatic_frames import (
    activities as automatic_frames_module,
)
from fishsense_services_processor.automatic_frames.activities import (
    AutomaticFramesActivities,
    LaserDot,
)
from fishsense_services_processor.automatic_frames.workflow import (
    FRAME_ATTEMPTS,
    PredictAutomaticFramesWorkflow,
)
from fishsense_services_processor.automatic_results.frames import (
    automatic_frame_result,
)
from fishsense_core.fish.fish_at_dot import crop_origin

FRAME_W, FRAME_H = 4014, 3016
CROP_W, CROP_H = 1800, 1350
DOT = (2000.0, 1500.0)
CAPTURE = uuid.uuid4()
TENANT = uuid.uuid4()
K = [[2850.0, 0.0, 2007.0], [0.0, 2850.0, 1508.0], [0.0, 0.0, 1.0]]


def _jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((FRAME_H, FRAME_W, 3), 40, np.uint8))
    assert ok
    return buf.tobytes()


def _mask_at(x, y, half_len=200, half_h=60):
    ox, oy = crop_origin(*DOT, FRAME_W, FRAME_H, CROP_W, CROP_H)
    m = np.zeros((CROP_H, CROP_W), np.uint8)
    cv2.ellipse(m, (int(x - ox), int(y - oy)), (half_len, half_h), 0, 0, 360, 1, -1)
    return m


class _ScoredSeam:
    """core's `ScoredSegmenter` needs both seams: `segment` is `segment_scored`'s
    masks, written once."""

    def segment(self, image):
        return [mask for mask, _ in self.segment_scored(image)]


class _Scored(_ScoredSeam):
    """A scored segmenter: (mask, score) pairs, crop-local."""

    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = 0

    def segment_scored(self, image):
        self.calls += 1
        assert image.shape[:2] == (CROP_H, CROP_W)
        return self.pairs


def _result(segmenter, dot=DOT, **kwargs):
    return automatic_frame_result(
        capture_id=CAPTURE,
        dot=None if dot is None else LaserDot(dot[0], dot[1], 0.93, 3, "laser@x"),
        jpeg_bytes=_jpeg(),
        segmenter=segmenter,
        checkpoint="sam3/3.1@abc",
        core_version="4.1.0",
        **kwargs,
    )


# -- the kernel ------------------------------------------------------------------


def test_a_fish_at_the_automatic_dot_is_keypointed_with_its_score():
    r = _result(_Scored([(_mask_at(*DOT), 0.81)]))

    assert r.status == "predicted"
    assert r.predictor_version == AUTOMATIC_HEADTAIL_PREDICTOR_VERSION
    assert r.sam_score == pytest.approx(0.81)
    assert (r.laser_x, r.laser_y, r.laser_confidence) == (*DOT, 0.93)
    assert (r.laser_predictor_version, r.laser_checkpoint) == (3, "laser@x")
    assert r.head_x == pytest.approx(DOT[0], abs=260)
    assert r.mask_bbox is not None and r.checkpoint == "sam3/3.1@abc"


def test_a_mask_below_the_gate_is_not_kept():
    r = _result(_Scored([(_mask_at(*DOT), 0.49)]))
    assert r.status == "no_detections"
    assert r.sam_score is None and r.head_x is None


def test_the_gate_is_inclusive():
    assert _result(_Scored([(_mask_at(*DOT), 0.5)])).status == "predicted"


def test_a_confident_fish_off_the_dot_does_not_count():
    """The dot must be on the mask; a weak mask under it is not kept either."""
    r = _result(
        _Scored([(_mask_at(*DOT), 0.3), (_mask_at(DOT[0] + 600, DOT[1]), 0.95)])
    )
    assert r.status == "laser_off_all_fish"


def test_the_kept_masks_score_is_the_one_under_the_dot():
    r = _result(
        _Scored([(_mask_at(DOT[0] + 600, DOT[1]), 0.95), (_mask_at(*DOT), 0.7)])
    )
    assert r.status == "predicted" and r.sam_score == pytest.approx(0.7)


class _BrokenScores(_ScoredSeam):
    """A SAM build whose mask and score counts disagree: core's segmenter
    raises ValueError before the gate."""

    def segment_scored(self, image):
        raise ValueError("SAM 3.1 returned 2 masks but 1 scores")


@pytest.mark.parametrize(
    "segmenter",
    [
        _Scored([(_mask_at(*DOT), None)]),
        _Scored([(_mask_at(*DOT), float("nan"))]),
        _BrokenScores(),
    ],
    ids=["no-score", "nan-score", "counts-disagree"],
)
def test_a_mask_whose_score_is_unusable_is_its_own_abstention(segmenter, caplog):
    """The gate is SAM's score on the mask under the dot (core #93). A score
    that is missing, NaN, or not one per mask cannot be gated: core raises
    ValueError, and the frame records `mask_unscored`, logged (review of
    #349). Raised out of the activity, the frame would retry until the child
    timed out and stall the dive's backlog behind it."""
    with caplog.at_level("WARNING"):
        r = _result(segmenter)

    assert r.status == "mask_unscored"
    assert r.sam_score is None
    assert (r.head_x, r.head_y, r.tail_x, r.tail_y) == (None, None, None, None)
    assert r.laser_x == DOT[0]  # the dot itself is still the detector's
    assert any("unscored" in rec.getMessage() for rec in caplog.records)


def test_no_dot_is_an_abstention_and_nothing_is_segmented():
    seg = _Scored([(_mask_at(*DOT), 0.9)])
    r = _result(seg, dot=None)
    assert r.status == "no_laser_dot" and seg.calls == 0


def test_a_slate_frame_keeps_its_dot_and_is_never_segmented():
    seg = _Scored([(_mask_at(*DOT), 0.9)])
    r = _result(seg, is_slate=True, slate_probability=0.97)
    assert (r.status, r.laser_x, r.slate_probability) == ("slate_frame", DOT[0], 0.97)
    assert seg.calls == 0


def test_a_slate_frame_with_no_dot_is_no_dot():
    r = _result(_Scored([]), dot=None, is_slate=True, slate_probability=0.9)
    assert r.status == "no_laser_dot" and r.slate_probability == 0.9


# -- a fish longer than the window (2026-10-22) -----------------------------------
#
# The window is 1800x1350 around the dot; a fish longer than it came back cut by
# the window's straight edges, and its head and tail were read off the cut
# (lab mission 383: a 686 mm Nassau Grouper measured 554 mm). A mask touching an
# edge of the window that is not the frame's own is segmented again on the
# whole frame.


def _fish_in_frame(cx, cy, half_len, half_h=150):
    m = np.zeros((FRAME_H, FRAME_W), np.uint8)
    cv2.ellipse(m, (int(cx), int(cy)), (half_len, half_h), 0, 0, 360, 1, -1)
    return m


def _window_of(frame_mask, dot=DOT):
    ox, oy = crop_origin(*dot, FRAME_W, FRAME_H, CROP_W, CROP_H)
    return np.ascontiguousarray(frame_mask[oy : oy + CROP_H, ox : ox + CROP_W])


class _Windowed(_ScoredSeam):
    """A scored segmenter that answers the window and the whole frame each
    with their own masks, as SAM would see them."""

    def __init__(self, window, frame):
        self.answers = {(CROP_H, CROP_W): window, (FRAME_H, FRAME_W): frame}
        self.shapes = []

    def segment_scored(self, image):
        self.shapes.append(image.shape[:2])
        return self.answers[image.shape[:2]]


LONG_FISH = _fish_in_frame(*DOT, half_len=1300)


def test_a_fish_longer_than_the_window_is_measured_on_the_whole_frame():
    seg = _Windowed([(_window_of(LONG_FISH), 0.93)], [(LONG_FISH, 0.88)])

    r = _result(seg)

    assert seg.shapes == [(CROP_H, CROP_W), (FRAME_H, FRAME_W)]
    assert r.status == "predicted"
    assert (r.crop_x, r.crop_y) == (0, 0)
    assert r.mask_bbox[0] <= DOT[0] - 1290 and r.mask_bbox[2] >= DOT[0] + 1290
    ends = sorted((r.head_x, r.tail_x))
    assert ends[0] == pytest.approx(DOT[0] - 1300, abs=30)
    assert ends[1] == pytest.approx(DOT[0] + 1300, abs=30)
    assert r.sam_score == pytest.approx(0.88)  # the whole frame's mask's


def test_a_fish_inside_the_window_is_segmented_once():
    fish = _fish_in_frame(*DOT, half_len=500)
    seg = _Windowed([(_window_of(fish), 0.9)], [])

    r = _result(seg)

    assert seg.shapes == [(CROP_H, CROP_W)]
    assert r.status == "predicted"
    assert (r.crop_x, r.crop_y) == crop_origin(*DOT, FRAME_W, FRAME_H, CROP_W, CROP_H)


def test_a_mask_cut_only_by_the_frames_own_edge_is_not_segmented_again():
    """Near the frame's edge the window is clamped there: a fish running off
    the photo is cut by the photo, which a bigger window cannot help."""
    dot = (300.0, 1500.0)
    fish = _fish_in_frame(150, 1500, half_len=600)  # runs off the left edge
    seg = _Windowed([(_window_of(fish, dot), 0.9)], [])

    r = _result(seg, dot=dot)

    assert seg.shapes == [(CROP_H, CROP_W)]
    assert r.status == "predicted" and r.crop_x == 0


def test_a_cut_fish_the_whole_frame_puts_no_mask_under_the_dot_is_an_abstention():
    """The cut mask's length is known to be wrong, so it is never kept."""
    elsewhere = _fish_in_frame(DOT[0] + 1500, 400, half_len=300)
    seg = _Windowed([(_window_of(LONG_FISH), 0.93)], [(elsewhere, 0.9)])

    r = _result(seg)

    assert r.status == "laser_off_all_fish"
    assert r.head_x is None and r.sam_score is None


def test_a_cut_fish_whose_whole_frame_mask_fails_the_gate_is_no_detection():
    seg = _Windowed([(_window_of(LONG_FISH), 0.93)], [(LONG_FISH, 0.4)])

    assert _result(seg).status == "no_detections"


# -- the activity ------------------------------------------------------------------


def _ref(folder: str) -> ObjectRef:
    return ObjectRef(
        bucket="b", key=f"tenants/{TENANT}/{folder}/{uuid.uuid4().hex}.JPG"
    )


class _Store:
    def __init__(self, *, missing=False):
        self.missing = missing
        self.uploaded = {}
        self.read = []

    async def download_raw(self, ref, directory: Path) -> Path:
        if self.missing:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        path = directory / "frame.ORF"
        path.write_bytes(b"raw")
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.uploaded[ref.key] = data

    async def download_image(self, ref=None, url=None):
        self.read.append(url)
        return _jpeg()


def _corrupt_raw(*args):
    raise rawpy.LibRawError("Input data is corrupted")


def _activities(
    store, *, gpu=True, dot=DOT, segmenter=None, predict_dot=None, render=None,
    put=None,
):  # fmt: skip

    async def checkpoint():
        return Path("/w/sam3.pt"), "sam3/3.1@abc"

    return AutomaticFramesActivities(
        store_factory=lambda: store,
        sam3_checkpoint=checkpoint,
        cuda_available=lambda: gpu,
        predict_dot=predict_dot
        or (
            lambda raw_path, camera, dist: (
                None if dot is None else LaserDot(dot[0], dot[1], 0.9, 3, "laser@x")
            )
        ),
        render_jpeg=render or (lambda raw_bytes, camera, dist: _jpeg()),
        segmenter=lambda path: segmenter or _Scored([(_mask_at(*DOT), 0.8)]),
        put=put or _no_put,
    )


def _no_put(url, headers, data):
    raise AssertionError("no mask was to be stored")


CAMERA = uuid.UUID(int=11)
RENDER = automatic_jpeg_folder(AUTOMATIC_JPEG_RENDER_VERSION, CAMERA)


def _payload(camera=CAMERA, **frame) -> PredictAutomaticFrameInput:
    """A contract-8 frame by default: the render at this version under
    `camera`'s key. ``jpeg_render_version=None`` makes it contract 7's."""
    frame.setdefault("jpeg_render_version", AUTOMATIC_JPEG_RENDER_VERSION)
    frame.setdefault("jpeg", _ref(RENDER if frame["jpeg_render_version"] else
                                  "preprocess_headtail_jpeg"))  # fmt: skip
    return PredictAutomaticFrameInput(
        frame=AutomaticFrame(
            capture_id=CAPTURE, raw=ObjectRef(bucket="s", key="raw/x.ORF"), **frame,
        ),
        camera_matrix=K, distortion_coefficients=[0.0] * 5,
        laser_region=[[0, 0], [FRAME_W, 0], [FRAME_W, FRAME_H], [0, FRAME_H]],
        camera_calibration_id=camera,
    )  # fmt: skip


async def test_the_activity_chains_dot_mask_and_head_tail():
    store = _Store()
    acts = _activities(store)
    r = await ActivityEnvironment().run(acts.predict_automatic_frame, _payload())
    assert r.status == "predicted" and r.sam_score == pytest.approx(0.8)
    assert list(store.uploaded.values()) == [_jpeg()]


URL = "https://garage/fishsense-tenant-lab/renders/k.JPG?sig"


def _rendered(**frame) -> PredictAutomaticFrameInput:
    """Contract 9: the capture's render is in its tenant's bucket, by URL."""
    return _payload(jpeg=None, render_url=URL, **frame)


async def test_sam_segments_the_captures_render_read_by_url():
    """The render stage made the image (2026-10-19): the frames stage reads
    it, renders nothing and writes nothing, and names the render it read."""
    store = _Store()
    rendered = []
    acts = _activities(store, render=lambda *a: rendered.append(a) or _jpeg())

    r = await ActivityEnvironment().run(acts.predict_automatic_frame, _rendered())

    assert store.read == [URL] and rendered == [] and store.uploaded == {}
    assert r.status == "predicted" and r.sam_score == pytest.approx(0.8)
    assert (r.jpeg_render_version, r.jpeg_camera_calibration_id) == (
        AUTOMATIC_JPEG_RENDER_VERSION,
        CAMERA,
    )


async def test_a_render_read_by_url_still_needs_the_raw_for_the_dot():
    r = await ActivityEnvironment().run(
        _activities(_Store(missing=True)).predict_automatic_frame, _rendered()
    )
    assert r.status == "raw_unavailable"


async def test_the_render_is_written_and_named_by_version_and_camera():
    """The JPEG the dot and mask were found in is stored, and the row says
    which render it is (2026-10-19). Written whatever `write_jpeg` says: from
    contract 8 the render version alone decides (review of #136)."""
    store = _Store()
    payload = _payload(write_jpeg=False)
    r = await ActivityEnvironment().run(
        _activities(store).predict_automatic_frame, payload
    )
    assert store.uploaded == {payload.frame.jpeg.key: _jpeg()}
    assert r.status == "predicted"
    assert r.jpeg_render_version == AUTOMATIC_JPEG_RENDER_VERSION
    assert r.jpeg_camera_calibration_id == CAMERA


@pytest.mark.parametrize(
    "payload",
    [
        _payload(jpeg_render_version=AUTOMATIC_JPEG_RENDER_VERSION + 1,
                 jpeg=_ref(automatic_jpeg_folder(AUTOMATIC_JPEG_RENDER_VERSION + 1,
                                                 CAMERA))),
        _payload(jpeg=_ref(automatic_jpeg_folder(AUTOMATIC_JPEG_RENDER_VERSION,
                                                 uuid.UUID(int=12)))),
        _payload(jpeg=_ref("preprocess_headtail_jpeg")),
        _payload(camera=None),
    ],
    ids=["another-version", "another-cameras-key", "not-a-render-key", "no-camera"],
)  # fmt: skip
async def test_a_frame_for_another_render_is_refused_and_retried(payload):
    """A processor at another render version (a lab worker deployed by hand),
    or a key that is not this render's, must not get this render written to
    it; retried, the frame reaches a worker at the asked version."""
    store = _Store()
    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(
            _activities(store).predict_automatic_frame, payload
        )
    assert error.value.type == "RenderVersionMismatch"
    assert not error.value.non_retryable
    assert store.uploaded == {}


@pytest.mark.parametrize("write", [True, False])
async def test_a_frame_resolved_before_the_render_was_versioned_runs_as_it_did(write):
    """Contract 7 (in flight across the deploy): no render named. It runs as
    it did -- the render written only if asked -- and names no render, so the
    orchestrator leaves it owed rather than failing the dive (review of #136)."""
    store = _Store()
    payload = _payload(jpeg_render_version=None, write_jpeg=write, camera=None)
    r = await ActivityEnvironment().run(
        _activities(store).predict_automatic_frame, payload
    )
    assert r.status == "predicted"
    assert store.uploaded == ({payload.frame.jpeg.key: _jpeg()} if write else {})
    assert (r.jpeg_render_version, r.jpeg_camera_calibration_id) == (None, None)


async def test_a_dot_outside_the_laser_region_is_no_dot():
    acts = _activities(_Store(), dot=(10.0, 10.0))
    payload = _payload()
    payload.laser_region = [[1000, 1000], [3000, 1000], [3000, 2000], [1000, 2000]]
    r = await ActivityEnvironment().run(acts.predict_automatic_frame, payload)
    assert r.status == "no_laser_dot" and r.laser_x is None


async def test_a_raw_missing_from_scratch_is_an_abstention():
    r = await ActivityEnvironment().run(
        _activities(_Store(missing=True)).predict_automatic_frame, _payload()
    )
    assert r.status == "raw_unavailable"


@pytest.mark.parametrize("stage", ["dot", "render"])
async def test_a_raw_that_will_not_decode_is_an_abstention(stage):
    """A corrupt raw must be recorded, not raised: raised, the activity retries
    to the child's 6 h timeout, the parent fails, and the next hourly run picks
    the same dive -- one bad file stalls the whole backlog. The slate detector
    records the same as `decode_failed`."""
    kwargs = (
        {"predict_dot": _corrupt_raw} if stage == "dot" else {"render": _corrupt_raw}
    )
    r = await ActivityEnvironment().run(
        _activities(_Store(), **kwargs).predict_automatic_frame, _payload()
    )
    assert r.status == "decode_failed"
    assert r.capture_id == CAPTURE
    assert r.jpeg_render_version is None


async def test_no_gpu_is_a_final_refusal():
    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(
            _activities(_Store(), gpu=False).predict_automatic_frame, _payload()
        )
    assert error.value.type == "NoGpuForSam3" and error.value.non_retryable


async def test_the_workflow_fans_out_one_activity_per_frame():
    from temporalio import activity

    seen = []

    @activity.defn(name="predict_automatic_frame")
    async def stub(payload: PredictAutomaticFrameInput) -> AutomaticFrameResult:
        seen.append(payload.frame.capture_id)
        # Each frame carries the camera its render is keyed under.
        assert payload.camera_calibration_id == CAMERA
        return AutomaticFrameResult(
            capture_id=payload.frame.capture_id, status="no_laser_dot",
            predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )  # fmt: skip

    frames = [
        AutomaticFrame(capture_id=uuid.uuid4(), raw=ObjectRef(bucket="s", key="r"),
                       jpeg=_ref("preprocess_headtail_jpeg"))  # fmt: skip
        for _ in range(3)
    ]
    payload = PredictAutomaticFramesInput(
        tenant_id=TENANT, mission_id=uuid.uuid4(), camera_matrix=K,
        distortion_coefficients=[0.0] * 5, frames=frames,
        camera_calibration_id=CAMERA,
    )  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-auto", activities=[stub],
                          workflows=[PredictAutomaticFramesWorkflow]):  # fmt: skip
            results = await env.client.execute_workflow(
                PredictAutomaticFramesWorkflow.run, payload,
                id=f"t-auto-{uuid.uuid4()}", task_queue="t-auto",
                result_type=List[AutomaticFrameResult],
            )  # fmt: skip

    assert [r.capture_id for r in results] == [f.capture_id for f in frames]
    assert sorted(seen) == sorted(f.capture_id for f in frames)


async def test_a_frame_that_keeps_failing_does_not_lose_the_others():
    """A frame no worker can finish (a crash, an OOM) was retried without
    limit until the child's timeout killed it, and every other frame of the
    dive with it (2026-10-15 gap audit). It is tried a bounded number of
    times, then left out: the rest come back, and the orchestrator records
    the dive's failure for the frames still owed."""
    from temporalio import activity

    bad = uuid.uuid4()
    attempts = []

    @activity.defn(name="predict_automatic_frame")
    async def stub(payload: PredictAutomaticFrameInput) -> AutomaticFrameResult:
        if payload.frame.capture_id == bad:
            attempts.append(1)
            raise RuntimeError("CUDA out of memory")
        return AutomaticFrameResult(
            capture_id=payload.frame.capture_id, status="no_laser_dot",
            predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )  # fmt: skip

    good = [uuid.uuid4(), uuid.uuid4()]
    frames = [
        AutomaticFrame(capture_id=c, raw=ObjectRef(bucket="s", key="r"),
                       jpeg=_ref("preprocess_headtail_jpeg"))  # fmt: skip
        for c in (good[0], bad, good[1])
    ]
    payload = PredictAutomaticFramesInput(
        tenant_id=TENANT, mission_id=uuid.uuid4(), camera_matrix=K,
        distortion_coefficients=[0.0] * 5, frames=frames,
    )  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-auto", activities=[stub],
                          workflows=[PredictAutomaticFramesWorkflow]):  # fmt: skip
            results = await env.client.execute_workflow(
                PredictAutomaticFramesWorkflow.run, payload,
                id=f"t-auto-{uuid.uuid4()}", task_queue="t-auto",
                result_type=List[AutomaticFrameResult],
                execution_timeout=timedelta(hours=1),
            )  # fmt: skip

    assert [r.capture_id for r in results] == good
    assert len(attempts) == FRAME_ATTEMPTS


async def test_no_gpu_fails_the_whole_run_with_its_cause():
    """`NoGpuForSam3` is the worker's, not a frame's: every frame would
    refuse, so leaving each out would record the dive's every frame as a
    frame failure and lose the cause (review of #85). The run fails with it."""
    from temporalio import activity
    from temporalio.client import WorkflowFailureError

    @activity.defn(name="predict_automatic_frame")
    async def stub(payload: PredictAutomaticFrameInput) -> AutomaticFrameResult:
        raise ApplicationError("no GPU", type="NoGpuForSam3", non_retryable=True)

    payload = PredictAutomaticFramesInput(
        tenant_id=TENANT, mission_id=uuid.uuid4(), camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        frames=[AutomaticFrame(capture_id=uuid.uuid4(), raw=ObjectRef(bucket="s", key="r"),
                               jpeg=_ref("preprocess_headtail_jpeg"))],
    )  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-auto", activities=[stub],
                          workflows=[PredictAutomaticFramesWorkflow]):  # fmt: skip
            with pytest.raises(WorkflowFailureError) as failure:
                await env.client.execute_workflow(
                    PredictAutomaticFramesWorkflow.run, payload,
                    id=f"t-auto-{uuid.uuid4()}", task_queue="t-auto",
                    result_type=List[AutomaticFrameResult],
                    execution_timeout=timedelta(hours=1),
                )  # fmt: skip
    assert "no GPU" in str(failure.value.cause.cause or failure.value.cause)


@pytest.mark.parametrize(
    "error_type",
    ["Sam3WeightsCorrupt", "Sam3SettingsInvalid", "LaserDetectorWeightsUnavailable",
     "LaserDetectorNotInManifest"],
)  # fmt: skip
async def test_a_weights_failure_fails_the_run_and_leaves_no_frame_out(error_type):
    """2026-10-24: a model's weights that cannot load are the worker's, as
    no GPU is. Left out, every frame of the mission was recorded as a frame
    failure (0044) and, at three runs, given up as ``frame_failed`` -- for a
    fault in the pod, not the frames. The run fails with the cause, once."""
    from temporalio import activity
    from temporalio.client import WorkflowFailureError

    attempts = []

    @activity.defn(name="predict_automatic_frame")
    async def stub(payload: PredictAutomaticFrameInput) -> AutomaticFrameResult:
        attempts.append(1)
        raise ApplicationError("weights", type=error_type, non_retryable=True)

    payload = PredictAutomaticFramesInput(
        tenant_id=TENANT, mission_id=uuid.uuid4(), camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        frames=[AutomaticFrame(capture_id=uuid.uuid4(), raw=ObjectRef(bucket="s", key="r"),
                               jpeg=_ref("preprocess_headtail_jpeg"))
                for _ in range(2)],
    )  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-auto", activities=[stub],
                          workflows=[PredictAutomaticFramesWorkflow]):  # fmt: skip
            with pytest.raises(WorkflowFailureError) as failure:
                await env.client.execute_workflow(
                    PredictAutomaticFramesWorkflow.run, payload,
                    id=f"t-auto-{uuid.uuid4()}", task_queue="t-auto",
                    result_type=List[AutomaticFrameResult],
                    execution_timeout=timedelta(hours=1),
                )  # fmt: skip
    assert failure.value.cause.cause.type == error_type
    assert len(attempts) == 2, "one try a frame: not retried"


async def test_a_run_from_before_the_wider_refusals_replays_as_it_ran(monkeypatch):
    """Review of #364: which failures fail the run is workflow code. A run in
    flight at the deploy, whose frame the code before left out for
    `Sam3WeightsCorrupt` (only `NoGpuForSam3` failed a run then), must replay
    under the new code as it ran -- left out, completed -- not raise. The
    history is recorded with the lookup returning the old list and no patch
    marker, as the code before ran."""
    from temporalio import activity
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    from fishsense_services_processor.automatic_frames import workflow as wf

    @activity.defn(name="predict_automatic_frame")
    async def stub(payload: PredictAutomaticFrameInput) -> AutomaticFrameResult:
        raise ApplicationError("weights", type="Sam3WeightsCorrupt", non_retryable=True)

    payload = PredictAutomaticFramesInput(
        tenant_id=TENANT, mission_id=uuid.uuid4(), camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        frames=[AutomaticFrame(capture_id=uuid.uuid4(), raw=ObjectRef(bucket="s", key="r"),
                               jpeg=_ref("preprocess_headtail_jpeg"))],
    )  # fmt: skip
    monkeypatch.setattr(wf, "refusals", lambda widened, legacy: legacy)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-auto-old", activities=[stub],
                          workflows=[PredictAutomaticFramesWorkflow],
                          workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            handle = await env.client.start_workflow(
                PredictAutomaticFramesWorkflow.run, payload,
                id=f"t-auto-old-{uuid.uuid4()}", task_queue="t-auto-old",
                execution_timeout=timedelta(hours=1),
            )  # fmt: skip
            assert await handle.result() == [], "the code before left it out"
            history = await handle.fetch_history()
    monkeypatch.undo()

    replayed = await Replayer(
        workflows=[PredictAutomaticFramesWorkflow], data_converter=DATA_CONVERTER
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_each_frame_logs_its_peak_gpu_memory(monkeypatch, caplog):
    """Review of #110: the GPU cap rests on how much one SAM 3.1 frame takes,
    which nobody had measured. Each frame logs the process's peak GPU memory
    over it (all frames in flight, at a cap above one), so the cap is tuned
    from production, not guessed."""
    from fishsense_services_processor.automatic_frames import activities as acts

    monkeypatch.setattr(acts, "_reset_gpu_peak", lambda: None)
    monkeypatch.setattr(acts, "_gpu_peak_bytes", lambda: 5 * 2**30)
    with caplog.at_level("INFO"):
        await ActivityEnvironment().run(
            _activities(_Store()).predict_automatic_frame, _payload()
        )
    assert any("peak_gpu_gib=5.00" in r.getMessage() for r in caplog.records)


class _CudaDies(_ScoredSeam):
    def segment_scored(self, image):
        raise RuntimeError("CUDA error: unspecified launch failure")


class _SomethingElse(_ScoredSeam):
    def segment_scored(self, image):
        raise RuntimeError("shapes do not match")


async def test_a_cuda_failure_restarts_the_worker_and_still_reports_the_frame():
    """A CUDA launch failure leaves the process's context unusable: on
    2026-10-19 one fault made every frame after it fail on the same pod. The
    frame's failure is still reported (it retries), and the worker is told to
    exit, so Kubernetes restarts it with a clean context."""
    exits = []
    acts = _activities(_Store(), segmenter=_CudaDies())
    acts._on_cuda_failure = lambda: exits.append(1)
    with pytest.raises(RuntimeError, match="CUDA error"):
        await ActivityEnvironment().run(acts.predict_automatic_frame, _payload())
    assert exits == [1]


async def test_any_other_failure_leaves_the_worker_running():
    exits = []
    acts = _activities(_Store(), segmenter=_SomethingElse())
    acts._on_cuda_failure = lambda: exits.append(1)
    with pytest.raises(RuntimeError, match="shapes"):
        await ActivityEnvironment().run(acts.predict_automatic_frame, _payload())
    assert exits == []


# -- the stored mask (contract 12, 2026-10-21) ------------------------------------


MASK_KEY = mask_key("automatic", CAPTURE, uuid.UUID(int=5))


def _with_mask(**frame) -> PredictAutomaticFrameInput:
    return _rendered(mask=MaskUpload(key=MASK_KEY, put_url="https://g/mask"), **frame)


async def test_a_kept_mask_is_stored_in_the_apps_encoding_and_named():
    puts = []

    def put(url, headers, data):
        puts.append((url, data))
        return 200

    r = await ActivityEnvironment().run(
        _activities(_Store(), put=put).predict_automatic_frame, _with_mask()
    )

    ((url, data),) = puts
    pixels = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    ox, oy = crop_origin(*DOT, FRAME_W, FRAME_H, CROP_W, CROP_H)
    expected = np.zeros((FRAME_H, FRAME_W), np.uint8)
    expected[oy : oy + CROP_H, ox : ox + CROP_W] = _mask_at(*DOT) * 255
    assert url == "https://g/mask" and (pixels == expected).all()
    assert r.status == "predicted"
    assert (r.mask.key, r.mask.size, r.mask.sha256) == (
        MASK_KEY, len(data), hashlib.sha256(data).hexdigest(),
    )  # fmt: skip
    assert (r.mask.width, r.mask.height) == (FRAME_W, FRAME_H)
    assert np.count_nonzero(pixels) == r.mask_area_px


async def test_a_long_fishs_stored_mask_is_the_whole_frames():
    puts = []

    def put(url, headers, data):
        puts.append(data)
        return 200

    seg = _Windowed([(_window_of(LONG_FISH), 0.93)], [(LONG_FISH, 0.88)])
    r = await ActivityEnvironment().run(
        _activities(_Store(), put=put, segmenter=seg).predict_automatic_frame,
        _with_mask(),
    )

    (data,) = puts
    pixels = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    assert r.status == "predicted"
    assert (pixels == LONG_FISH * 255).all()


async def test_a_frame_with_no_upload_slot_stores_nothing():
    r = await ActivityEnvironment().run(
        _activities(_Store()).predict_automatic_frame, _rendered()
    )
    assert r.status == "predicted" and r.mask is None


@pytest.mark.parametrize(
    "segmenter, status",
    [(_Scored([]), "no_detections"),
     (_Scored([(_mask_at(*DOT), 0.3)]), "no_detections")],
)  # fmt: skip
async def test_a_frame_that_kept_no_mask_stores_nothing(segmenter, status):
    r = await ActivityEnvironment().run(
        _activities(_Store(), segmenter=segmenter).predict_automatic_frame,
        _with_mask(),
    )
    assert r.status == status and r.mask is None


async def test_an_unscored_mask_is_recorded_by_the_activity_and_not_stored():
    """The real path (review of #349): SAM hands back a mask with no score;
    the activity returns `mask_unscored` instead of raising, and stores no
    mask."""
    acts = _activities(_Store(), segmenter=_Scored([(_mask_at(*DOT), None)]))

    r = await ActivityEnvironment().run(acts.predict_automatic_frame, _with_mask())

    assert r.status == "mask_unscored" and r.mask is None


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
async def test_a_mask_that_cannot_be_stored_costs_only_itself(failure):
    """Review of #232: the mask is an addition. A PUT the store refuses, or
    can't be reached for, is logged; the frame's prediction stands, with no
    mask named -- not failed, and SAM not run again."""

    def put(url, headers, data):
        if failure == "unreachable":
            raise OSError("connection refused")
        return 403

    r = await ActivityEnvironment().run(
        _activities(_Store(), put=put).predict_automatic_frame, _with_mask()
    )
    assert r.status == "predicted" and r.mask is None


def test_the_shadow_adapter_refuses_masks_and_scores_that_do_not_pair():
    """`ScoredSam3Adapter.prompted` pairs each mask with its score through
    `pair_masks_and_scores`; a count mismatch would pair a mask with another's
    score, and the shadow stage would keep the wrong slate (review of #349).
    Core's segmenter refuses it; so does this."""
    from fishsense_services_processor.automatic_results.frames import (
        pair_masks_and_scores,
    )

    masks = [np.ones((4, 4), bool), np.zeros((4, 4), bool)]

    with pytest.raises(ValueError, match="2 masks but 1 scores"):
        pair_masks_and_scores(masks, [0.9])
    assert [s for _, s in pair_masks_and_scores(masks, [0.9, 0.2])] == [0.9, 0.2]


def test_the_shadow_adapter_refuses_missing_scores_before_copying_masks():
    from fishsense_services_processor.automatic_results.frames import (
        pair_masks_and_scores,
    )

    class _Mask:
        copied = 0

        def __array__(self, *args, **kwargs):
            _Mask.copied += 1
            return np.ones((4, 4), bool)

    with pytest.raises(ValueError, match="without scores"):
        pair_masks_and_scores([_Mask(), _Mask()], None)
    assert _Mask.copied == 0


# -- the dot's colour (contract v22, 2026-10-25) ---------------------------------
#
# The laser kernel reads the dot's colour off its pixels; the frames stage
# dropped it (`prediction, *_, checkpoint`), so no automatic row had one.


def _coloured(color="green", margin=0.31):
    return LaserDot(DOT[0], DOT[1], 0.93, 3, "laser@x", color, margin)


def test_the_dots_colour_is_kept_whatever_the_frame_becomes():
    fish = automatic_frame_result(
        capture_id=CAPTURE, dot=_coloured(), jpeg_bytes=_jpeg(),
        segmenter=_Scored([(_mask_at(*DOT), 0.81)]), checkpoint="sam3/3.1@abc",
        core_version="4.1.0",
    )  # fmt: skip
    slate = automatic_frame_result(
        capture_id=CAPTURE, dot=_coloured("red", 0.5), jpeg_bytes=_jpeg(),
        segmenter=_Scored([]), checkpoint="sam3/3.1@abc", core_version="4.1.0",
        is_slate=True, slate_probability=0.97,
    )  # fmt: skip

    assert (fish.laser_color, fish.laser_color_margin) == ("green", 0.31)
    assert (slate.laser_color, slate.laser_color_margin) == ("red", 0.5)


def test_a_dot_with_no_reading_has_no_colour():
    r = _result(_Scored([(_mask_at(*DOT), 0.81)]))

    assert (r.laser_color, r.laser_color_margin) == (None, None)


def test_the_stage_keeps_the_kernels_colour(monkeypatch):
    from types import SimpleNamespace

    from fishsense_services_processor.automatic_frames import stage
    from fishsense_services_processor.laser_predict import activities as laser

    prediction = SimpleNamespace(x=10.0, y=20.0, confidence=0.8)
    monkeypatch.setattr(
        laser,
        "_predict_from_raw",
        lambda *a: (prediction, 4000, 3000, "red", 0.42, "laser@x"),
    )

    dot = stage._predict_dot(Path("x.ORF"), [[1, 0, 0]], [0.0])

    assert (dot.color, dot.color_margin) == ("red", 0.42)
    assert (dot.x, dot.y, dot.checkpoint) == (10.0, 20.0, "laser@x")
