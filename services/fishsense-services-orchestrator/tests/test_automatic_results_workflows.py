"""The automatic-results workflows: one backlog dive per run, every step.

New in v2. Per dive, in order (cscw-fishsense2027@96a8da07 PAPER.md §6's
chain): the GPU frames step (dot, SAM 3.1 mask, head/tail) on staged raw
frames; the label-free calibration (per-image role); the lengths (light
role); then BioCLIP zero-shot species on the automatic masks (the species
stage's own processor workflow). **Species is last and cannot cost a dive its
lengths** (2026-10-15 audit: it ran second, unprovisioned, and its failure
failed every dive before a length was written). Pinned as the other parents
are:

* the GPU is woken only once there is GPU work, and **only a real GPU runs
  it**: the CPU fallback serves the queue with Mask R-CNN, whose lengths were
  never validated, so `cpu_fallback` (like `unavailable`) skips the GPU steps
  -- the CPU steps still run on what is already there;
* raw frames are staged before the frames child and cleaned up after it, the
  child named as a raw-scratch reader (so another stage's cleanup waits);
* a dive with no slate frame gets its refusal written without a processor;
* a child already running under another firing means its step is skipped;
* the parent selects; nothing selected is nothing done;
* **off by default** (test_automatic_results_stage.py).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List, Optional

from temporalio import activity, workflow
from fishsense_services_contracts.temporal import DATA_CONVERTER
from temporalio.exceptions import ApplicationError
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_contracts.automatic_results import (
    AutomaticCalibrationFrame,
    AutomaticCalibrationResult,
    AutomaticFrame,
    AutomaticFrameResult,
    AutomaticLength,
    AutomaticMeasureCapture,
    FitAutomaticCalibrationInput,
    MeasureAutomaticInput,
    MeasureAutomaticResult,
    PredictAutomaticFramesInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species_prediction import (
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesCandidate,
    SpeciesPredictionResult,
)
from fishsense_services_orchestrator.automatic_results.activities import (
    AutomaticCalibrationPlan,
    AutomaticMeasurePlan,
    AutomaticTarget,
)
from fishsense_services_orchestrator.automatic_results import workflow as sut
from fishsense_services_orchestrator.automatic_results.workflow import (
    AutomaticMissionReport,
    AutomaticResultsForMissionWorkflow,
    AutomaticResultsParentWorkflow,
    AutomaticRunReport,
)
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)

TENANT, MISSION = uuid.uuid4(), uuid.uuid4()
TARGET = AutomaticTarget(TENANT, MISSION)
QUEUE = "test-automatic-results"
K = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
REF = ObjectRef(bucket="b", key="k")
EVENTS: list[tuple] = []
#: What each cohort query was told to exclude, in turn.
SELECTS: list[tuple] = []


def _frames_input(n):
    return PredictAutomaticFramesInput(
        tenant_id=TENANT, mission_id=MISSION, camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        frames=[AutomaticFrame(capture_id=uuid.uuid4(), raw=REF, jpeg=REF)
                for _ in range(n)],
    )  # fmt: skip


def _species_input(n):
    return PredictSpeciesImagesInput(
        tenant_id=TENANT, mission_id=MISSION,
        candidates=[SpeciesCandidate(choice="Fish, A (A a)", scientific_name="A a")],
        images=[PredictSpeciesImage(capture_id=uuid.uuid4(),
                                    headtail_prediction_id=uuid.uuid4(), jpeg=REF,
                                    mask_bbox=[1, 2, 3, 4]) for _ in range(n)],
    )  # fmt: skip


def _calibration_plan(n):
    return AutomaticCalibrationPlan(
        payload=FitAutomaticCalibrationInput(
            tenant_id=TENANT, mission_id=MISSION, camera_matrix=K,
            frames=[AutomaticCalibrationFrame(capture_id=uuid.uuid4(), jpeg=REF,
                                              x=1.0, y=2.0) for _ in range(n)],
        ),
        camera_calibration_id=uuid.uuid4(),
    )  # fmt: skip


def _measure_plan(n):
    return AutomaticMeasurePlan(
        payload=MeasureAutomaticInput(
            tenant_id=TENANT, mission_id=MISSION, camera_matrix=K,
            laser_position=[0.1, 0, 0], laser_axis=[0, 0, 1],
            captures=[AutomaticMeasureCapture(
                capture_id=uuid.uuid4(), automatic_head_tail_prediction_id=uuid.uuid4(),
                laser_x=1, laser_y=2, head_x=3, head_y=4, tail_x=5, tail_y=6,
            ) for _ in range(n)],
        ),
        calibration_source="stored", automatic_laser_calibration_id=None,
        laser_calibration_id=uuid.uuid4(), camera_calibration_id=None,
    )  # fmt: skip


def _stubs(*, mode="gpu", frames=2, species=1, slate=8, plan=1, selected=TARGET):
    """`selected` is the cohort's answer every time, or a list of answers in
    turn (None once it runs out)."""
    cohort = list(selected) if isinstance(selected, list) else None

    @activity.defn(name="select_next_dive_for_automatic_results")
    async def select(exclude: List[uuid.UUID]) -> Optional[AutomaticTarget]:
        SELECTS.append(tuple(exclude))
        if cohort is None:
            return selected
        return cohort.pop(0) if cohort else None

    @activity.defn(name="record_automatic_results_failure")
    async def failed(t: AutomaticTarget, step: str, error: str) -> None:
        EVENTS.append(("failed", t.mission_id, step))

    @activity.defn(name="record_automatic_frame_failures")
    async def frames_failed(
        t: AutomaticTarget, captures: List[uuid.UUID], error: str
    ) -> List[uuid.UUID]:
        EVENTS.append(("frame failures", len(captures)))
        return []

    @activity.defn(name="resolve_automatic_frames_inputs")
    async def frames_in(t: AutomaticTarget) -> PredictAutomaticFramesInput:
        return _frames_input(frames)

    @activity.defn(name="persist_automatic_frames")
    async def frames_out(t: AutomaticTarget, r: List[AutomaticFrameResult]) -> int:
        EVENTS.append(("persist frames", len(r)))
        return len(r)

    @activity.defn(name="resolve_automatic_species_inputs")
    async def species_in(t: AutomaticTarget) -> PredictSpeciesImagesInput:
        return _species_input(species)

    @activity.defn(name="persist_automatic_species")
    async def species_out(t: AutomaticTarget, r: List[SpeciesPredictionResult]) -> int:
        EVENTS.append(("persist species", [x.status for x in r]))
        return len(r)

    @activity.defn(name="resolve_automatic_calibration_inputs")
    async def cal_in(t: AutomaticTarget) -> AutomaticCalibrationPlan:
        return _calibration_plan(slate)

    @activity.defn(name="persist_automatic_calibration")
    async def cal_out(t: AutomaticTarget, p: AutomaticCalibrationPlan,
                      r: AutomaticCalibrationResult) -> uuid.UUID:  # fmt: skip
        EVENTS.append(("persist calibration", r.outcome, r.refusal_reason))
        return uuid.uuid4()

    @activity.defn(name="resolve_automatic_measure_inputs")
    async def measure_in(t: AutomaticTarget) -> Optional[AutomaticMeasurePlan]:
        return None if plan is None else _measure_plan(plan)

    @activity.defn(name="persist_automatic_measurements")
    async def measure_out(t: AutomaticTarget, p: AutomaticMeasurePlan,
                          r: MeasureAutomaticResult) -> int:  # fmt: skip
        EVENTS.append(("persist lengths", len(r.lengths)))
        return len(r.lengths)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake_gpu() -> str:
        EVENTS.append(("wake gpu",))
        return mode

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake_per_image() -> None:
        EVENTS.append(("wake per-image",))

    @activity.defn(name="ensure_light_processor_running")
    async def wake_light() -> None:
        EVENTS.append(("wake light",))

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage(t: StagingTarget) -> StageRawBytesResult:
        EVENTS.append(("stage",))
        return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def cleanup(t: StagingTarget) -> CleanupRawBytesResult:
        EVENTS.append(("cleanup",))
        return CleanupRawBytesResult(deleted=1)

    return [select, failed, frames_failed, frames_in, frames_out, species_in, species_out, cal_in,
            cal_out, measure_in, measure_out, wake_gpu, wake_per_image, wake_light,
            stage, cleanup]  # fmt: skip


@activity.defn(name="_record")
async def _record(event: List[str]) -> None:
    EVENTS.append(tuple(event))


def _frame_results(payload):
    return [AutomaticFrameResult(capture_id=f["capture_id"], status="no_laser_dot",
                                 predictor_version=1).model_dump(mode="json")
            for f in payload["frames"]]  # fmt: skip


def _species_results(payload):
    return [SpeciesPredictionResult(capture_id=i["capture_id"],
                                    headtail_prediction_id=i["headtail_prediction_id"],
                                    status="decode_failed", predictor_version=1,
                                    model_id="m").model_dump(mode="json")
            for i in payload["images"]]  # fmt: skip


def _calibration_result(payload):
    return AutomaticCalibrationResult(
        mission_id=payload["dive_id"], outcome="accepted", algorithm_version="1",
        laser_position=[0.1, 0, 0], laser_axis=[0, 0, 1],
    ).model_dump(mode="json")  # fmt: skip


def _measure_result(payload):
    return MeasureAutomaticResult(
        mission_id=payload["dive_id"], algorithm="a", algorithm_version="1",
        core_version="4.1.0",
        lengths=[AutomaticLength(capture_id=c["capture_id"],
                                 automatic_head_tail_prediction_id=c[
                                     "automatic_head_tail_prediction_id"],
                                 length_m=0.3, depth_m=2.0)
                 for c in payload["captures"]],
    ).model_dump(mode="json")  # fmt: skip


async def _record_child() -> None:
    await workflow.execute_activity(
        "_record", ["child", workflow.info().workflow_id],
        schedule_to_close_timeout=timedelta(seconds=5),
    )  # fmt: skip


@workflow.defn(name="PredictAutomaticFramesWorkflow")
class _Frames:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        await _record_child()
        return _frame_results(payload)


@workflow.defn(name="PredictSpeciesImagesWorkflow")
class _Species:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        await _record_child()
        return _species_results(payload)


@workflow.defn(name="PredictAutomaticFramesWorkflow")
class _FailingFrames:  # pylint: disable=too-few-public-methods
    """A frame no worker can finish (or a raw gone from scratch)."""

    @workflow.run
    async def run(self, payload: dict) -> list:
        await _record_child()
        raise ApplicationError("cannot finish", type="Boom", non_retryable=True)


@workflow.defn(name="PredictAutomaticFramesWorkflow")
class _PartialFrames:  # pylint: disable=too-few-public-methods
    """One frame failed every try and is left out (the processor's rule)."""

    @workflow.run
    async def run(self, payload: dict) -> list:
        await _record_child()
        return _frame_results(payload)[1:]


@workflow.defn(name="PredictSpeciesImagesWorkflow")
class _FailingSpecies:  # pylint: disable=too-few-public-methods
    """BioCLIP unprovisioned: the processor's `BioclipSettingsInvalid`."""

    @workflow.run
    async def run(self, payload: dict) -> list:
        await _record_child()
        raise ApplicationError(
            "BIOCLIP pins missing", type="BioclipSettingsInvalid", non_retryable=True
        )


@workflow.defn(name="FitAutomaticCalibrationWorkflow")
class _Calibration:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> dict:
        await _record_child()
        return _calibration_result(payload)


@workflow.defn(name="MeasureAutomaticWorkflow")
class _Measure:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> dict:
        await _record_child()
        return _measure_result(payload)


async def _run(
    workflow_cls=AutomaticResultsForMissionWorkflow, *args, species_fails=False,
    frames_fail=False, frames_partial=False, **stubs,
):  # fmt: skip
    EVENTS.clear()
    SELECTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        workers = [
            Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                   workflows=[AutomaticResultsForMissionWorkflow,
                              AutomaticResultsParentWorkflow]),
            Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                   activities=[_record],
                   workflows=[_FailingFrames if frames_fail
                              else _PartialFrames if frames_partial else _Frames,
                              _FailingSpecies if species_fails else _Species]),
            Worker(env.client, task_queue=PROCESSOR_TASK_QUEUE, activities=[_record],
                   workflows=[_Calibration]),
            Worker(env.client, task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                   activities=[_record],
                   workflows=[_Measure]),
        ]  # fmt: skip
        async with workers[0], workers[1], workers[2], workers[3]:
            return await env.client.execute_workflow(
                workflow_cls.run, *args, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


async def test_one_mission_every_step_in_order():
    await _run(AutomaticResultsForMissionWorkflow, TARGET)

    assert EVENTS == [
        ("wake gpu",),
        ("stage",),
        ("child", f"automatic-frames-{MISSION}"),
        ("persist frames", 2),
        ("cleanup",),
        ("wake per-image",),
        ("child", f"automatic-calibration-{MISSION}"),
        ("persist calibration", "accepted", None),
        ("wake light",),
        ("child", f"automatic-measure-{MISSION}"),
        ("persist lengths", 1),
        ("child", f"automatic-species-{MISSION}"),
        ("persist species", ["decode_failed"]),
    ]


async def test_a_species_failure_costs_the_mission_nothing():
    """Species last, and its failure logged rather than raised: the dive's
    calibration and lengths are already written, and the run succeeds."""
    report = await _run(AutomaticResultsForMissionWorkflow, TARGET, species_fails=True)
    assert report == AutomaticMissionReport(TENANT, MISSION, species_failed=True)
    assert ("persist lengths", 1) in EVENTS
    assert ("child", f"automatic-species-{MISSION}") in EVENTS
    assert not [e for e in EVENTS if e[0] == "persist species"]


async def test_without_a_gpu_a_mission_owing_frames_waits():
    """Said, not silent (2026-10-15 gap audit). And its calibration and
    lengths wait too: on a dive whose frames are owed they would be fit on
    part of it, and every hour without a GPU appended another refusal to its
    history (review of #85)."""
    report = await _run(AutomaticResultsForMissionWorkflow, TARGET, mode="cpu_fallback")
    assert report.skipped == ["frames: no GPU (cpu_fallback)"]
    assert report.no_gpu == "cpu_fallback"
    kinds = [e[0] for e in EVENTS]
    assert "stage" not in kinds and "persist frames" not in kinds
    assert "persist calibration" not in kinds and "wake per-image" not in kinds
    assert "persist lengths" not in kinds and "persist species" not in kinds


async def test_without_a_gpu_a_mission_owing_no_frames_still_gets_its_lengths():
    """Its frames are all done: the CPU steps go on, and only species waits."""
    report = await _run(
        AutomaticResultsForMissionWorkflow, TARGET, mode="cpu_fallback", frames=0
    )
    assert report.skipped == ["species: no GPU (cpu_fallback)"]
    assert ("persist calibration", "accepted", None) in EVENTS
    assert ("persist lengths", 1) in EVENTS


async def test_no_gpu_work_wakes_no_gpu():
    report = await _run(AutomaticResultsForMissionWorkflow, TARGET, frames=0, species=0)
    assert ("wake gpu",) not in EVENTS and ("stage",) not in EVENTS
    assert report.skipped == []  # nothing owed is nothing skipped


async def test_no_slate_frame_is_a_refusal_without_a_processor():
    await _run(AutomaticResultsForMissionWorkflow, TARGET, slate=0)
    assert ("persist calibration", "refused", "no_candidates") in EVENTS
    assert ("wake per-image",) not in EVENTS


async def test_no_calibration_means_no_lengths():
    await _run(AutomaticResultsForMissionWorkflow, TARGET, plan=None)
    assert ("wake light",) not in EVENTS


async def test_the_parent_runs_the_selected_mission():
    report = await _run(AutomaticResultsParentWorkflow)
    assert report.missions == [AutomaticMissionReport(TENANT, MISSION)]
    assert ("persist lengths", 1) in EVENTS


async def test_nothing_selected_is_nothing_done():
    report = await _run(AutomaticResultsParentWorkflow, selected=None)
    assert report == AutomaticRunReport(missions=[], stopped="backlog empty")
    assert EVENTS == []


async def test_a_run_with_no_gpu_says_so_and_stops_draining():
    """Every backlog dive owes GPU work first (its frames). With no GPU the
    run walked the whole backlog doing nothing, and read as a quiet success
    (2026-10-15 gap audit): it stops at the first dive, and says why."""
    missions = [_target(), _target(), _target()]
    report = await _run(
        AutomaticResultsParentWorkflow, selected=missions, mode="cpu_fallback"
    )
    assert report.stopped == "no GPU (cpu_fallback)"
    assert [d.mission_id for d in report.missions] == [missions[0].mission_id]
    assert report.missions[0].no_gpu == "cpu_fallback"
    assert len(SELECTS) == 1


# -- the backlog drains; a failing dive does not hold it ----------------------------


def _target():
    return AutomaticTarget(TENANT, uuid.uuid4())


async def test_one_run_drains_the_backlog():
    """2026-10-15 audit: one dive per hourly firing put the ~474-dive backlog
    weeks out. A run takes dive after dive, telling the cohort what it took."""
    missions = [_target(), _target(), _target()]
    report = await _run(AutomaticResultsParentWorkflow, selected=missions)
    assert [d.mission_id for d in report.missions] == [d.mission_id for d in missions]
    assert report.stopped == "backlog empty"
    assert SELECTS == [(), (missions[0].mission_id,), (missions[0].mission_id, missions[1].mission_id),
                       tuple(d.mission_id for d in missions)]  # fmt: skip
    assert [e for e in EVENTS if e[0] == "persist lengths"] == [
        ("persist lengths", 1)
    ] * 3


async def test_a_mission_whose_step_fails_is_recorded_cleaned_up_and_passed_over():
    """A frame no worker can finish, a raw gone from the NAS: the failure is
    recorded (at three the cohort leaves the dive for an operator), its raw
    scratch is cleaned up, and the run goes on."""
    missions = [_target(), _target()]
    report = await _run(
        AutomaticResultsParentWorkflow, selected=missions, frames_fail=True
    )
    assert [d.failed for d in report.missions] == ["frames", "frames"]
    assert [e for e in EVENTS if e[0] == "failed"] == [
        ("failed", d.mission_id, "frames") for d in missions
    ]
    assert [e[0] for e in EVENTS].count("cleanup") == 2
    assert not [e for e in EVENTS if e[0] == "persist calibration"]


async def test_a_mission_handed_back_twice_ends_the_run():
    report = await _run(AutomaticResultsParentWorkflow)
    assert report.stopped == "dive handed back"
    assert len(SELECTS) == 2


async def test_no_new_mission_is_started_once_the_drain_window_has_passed():
    assert sut.AUTOMATIC_DRAIN_WINDOW <= timedelta(minutes=55)
    assert sut.RUN_TIMEOUT >= sut.AUTOMATIC_DRAIN_WINDOW


async def test_a_mission_run_by_hand_records_its_failure_and_fails():
    """The validation harness runs dives by hand: a failure must not read as
    a finished dive."""
    try:
        await _run(AutomaticResultsForMissionWorkflow, TARGET, frames_fail=True)
    except WorkflowFailureError:
        pass
    else:
        raise AssertionError("a failed dive run by hand must fail")
    assert ("failed", MISSION, "frames") in EVENTS
    assert ("cleanup",) in EVENTS


def test_the_calibration_child_outlives_the_fit_it_runs():
    """The fit may take up to the processor's FIT_TIMEOUT (it heartbeats
    throughout); the orchestrator must not cut its child off sooner."""
    from fishsense_services_processor.automatic_calibration.workflow import (
        FIT_TIMEOUT,
    )

    assert sut.CALIBRATION_CHILD_TIMEOUT > FIT_TIMEOUT
    assert sut.RUN_TIMEOUT >= sut.AUTOMATIC_DRAIN_WINDOW + sut.CALIBRATION_CHILD_TIMEOUT


async def test_the_frames_that_came_back_are_kept_and_the_rest_owed():
    """One frame no worker could finish no longer costs the dive its other
    frames (2026-10-15 gap audit): what came back is persisted, and the frame
    left out is recorded against the *frame* -- never one of the dive's
    strikes, which a transient fault would otherwise spend (review of #85);
    at three runs the store makes it a terminal abstention. The scratch is
    cleaned up, the dive's calibration waits for its frames, and the run
    goes on to the next dive."""
    other = _target()
    report = await _run(AutomaticResultsParentWorkflow, selected=[TARGET, other],
                        frames=3, frames_partial=True)  # fmt: skip
    assert ("persist frames", 2) in EVENTS
    assert ("frame failures", 1) in EVENTS
    assert not [e for e in EVENTS if e[0] == "failed"]
    assert ("cleanup",) in EVENTS
    assert not [e for e in EVENTS if e[0] == "persist calibration"]
    first = report.missions[0]
    assert first.failed is None and first.skipped == [
        "calibration, lengths, species: 1 frame still owed"
    ]
    assert [d.mission_id for d in report.missions] == [MISSION, other.mission_id]


def test_a_gpu_child_has_time_for_every_frame_it_was_given():
    """Review of #110: at one frame at a time (~18 s), a fixed 6 h ran out at
    ~1200 frames, and the largest dives have 1,698, 1,672, 1,498 (production,
    2026-10-18); the child returns only at the end, so everything computed was
    lost and the next firing started over. The budget scales with the frames,
    within a floor and a ceiling the run timeouts are built from."""
    from fishsense_services_orchestrator.automatic_results import run_missions

    assert sut.gpu_child_timeout(10) == sut.GPU_CHILD_MIN
    big = sut.gpu_child_timeout(1698)
    assert big >= timedelta(seconds=1698 * 18 * 2)  # twice the measured rate
    assert big <= sut.GPU_CHILD_MAX
    assert sut.gpu_child_timeout(10**6) == sut.GPU_CHILD_MAX
    assert sut.RUN_TIMEOUT >= 2 * sut.GPU_CHILD_MAX + sut.AUTOMATIC_DRAIN_WINDOW
    assert run_missions.MISSION_TIMEOUT >= sut.RUN_TIMEOUT
