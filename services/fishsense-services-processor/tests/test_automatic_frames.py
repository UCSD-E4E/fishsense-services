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
from fishsense_services_processor.headtail_predict.geometry import crop_origin

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


class _Scored:
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


def test_a_kept_mask_whose_score_cannot_be_named_is_not_predicted(monkeypatch, caplog):
    """The gate is SAM's score on the mask under the dot. If the kernel keeps
    a mask but the score can't be named for it (the kernel's crop and ours
    disagree), the row carried `predicted` with `sam_score` NULL -- and NULL
    passed the table's gate (2026-10-15 gap audit). It is its own abstention,
    `mask_unscored`, logged: a mismatch to count and fix, never the
    scientific answer `laser_off_all_fish` (review of #85)."""
    from fishsense_services_processor.automatic_results import frames

    monkeypatch.setattr(frames._Gated, "score_at", lambda self, point: None)

    with caplog.at_level("WARNING"):
        r = _result(_Scored([(_mask_at(*DOT), 0.81)]))

    assert r.status == "mask_unscored"
    assert any("unscored" in rec.getMessage() for rec in caplog.records)
    assert r.sam_score is None
    assert (r.head_x, r.head_y, r.tail_x, r.tail_y) == (None, None, None, None)
    assert r.laser_x == DOT[0]  # the dot itself is still the detector's


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


# -- the activity ------------------------------------------------------------------


def _ref(folder: str) -> ObjectRef:
    return ObjectRef(
        bucket="b", key=f"tenants/{TENANT}/{folder}/{uuid.uuid4().hex}.JPG"
    )


class _Store:
    def __init__(self, *, missing=False):
        self.missing = missing
        self.uploaded = {}

    async def download_raw(self, ref, directory: Path) -> Path:
        if self.missing:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        path = directory / "frame.ORF"
        path.write_bytes(b"raw")
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.uploaded[ref.key] = data


def _corrupt_raw(*args):
    raise rawpy.LibRawError("Input data is corrupted")


def _activities(
    store, *, gpu=True, dot=DOT, segmenter=None, predict_dot=None, render=None
):
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
    )


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


class _CudaDies:
    def segment_scored(self, image):
        raise RuntimeError("CUDA error: unspecified launch failure")


class _SomethingElse:
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
