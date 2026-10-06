"""The automatic-results workflows: one backlog dive per run, every step.

New in v2. Per dive, in order (cscw-fishsense2027@96a8da07 PAPER.md §6's
chain): the GPU frames step (dot, SAM 3.1 mask, head/tail) on staged raw
frames; BioCLIP zero-shot species on the automatic masks (the species stage's
own processor workflow); the label-free calibration (per-image role); the
lengths (light role). Pinned as the other parents are:

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
from temporalio.contrib.pydantic import pydantic_data_converter
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
from fishsense_services_orchestrator.automatic_results.workflow import (
    AutomaticResultsForDiveWorkflow,
    AutomaticResultsParentWorkflow,
)
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)

TENANT, DIVE = uuid.uuid4(), uuid.uuid4()
TARGET = AutomaticTarget(TENANT, DIVE)
QUEUE = "test-automatic-results"
K = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
REF = ObjectRef(bucket="b", key="k")
EVENTS: list[tuple] = []


def _frames_input(n):
    return PredictAutomaticFramesInput(
        tenant_id=TENANT, dive_id=DIVE, camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        frames=[AutomaticFrame(capture_id=uuid.uuid4(), raw=REF, jpeg=REF)
                for _ in range(n)],
    )  # fmt: skip


def _species_input(n):
    return PredictSpeciesImagesInput(
        tenant_id=TENANT, dive_id=DIVE,
        candidates=[SpeciesCandidate(choice="Fish, A (A a)", scientific_name="A a")],
        images=[PredictSpeciesImage(capture_id=uuid.uuid4(),
                                    headtail_prediction_id=uuid.uuid4(), jpeg=REF,
                                    mask_bbox=[1, 2, 3, 4]) for _ in range(n)],
    )  # fmt: skip


def _calibration_plan(n):
    return AutomaticCalibrationPlan(
        payload=FitAutomaticCalibrationInput(
            tenant_id=TENANT, dive_id=DIVE, camera_matrix=K,
            frames=[AutomaticCalibrationFrame(capture_id=uuid.uuid4(), jpeg=REF,
                                              x=1.0, y=2.0) for _ in range(n)],
        ),
        camera_calibration_id=uuid.uuid4(),
    )  # fmt: skip


def _measure_plan(n):
    return AutomaticMeasurePlan(
        payload=MeasureAutomaticInput(
            tenant_id=TENANT, dive_id=DIVE, camera_matrix=K,
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
    @activity.defn(name="select_next_dive_for_automatic_results")
    async def select() -> Optional[AutomaticTarget]:
        return selected

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

    return [select, frames_in, frames_out, species_in, species_out, cal_in, cal_out,
            measure_in, measure_out, wake_gpu, wake_per_image, wake_light, stage,
            cleanup]  # fmt: skip


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
        dive_id=payload["dive_id"], outcome="accepted", algorithm_version="1",
        laser_position=[0.1, 0, 0], laser_axis=[0, 0, 1],
    ).model_dump(mode="json")  # fmt: skip


def _measure_result(payload):
    return MeasureAutomaticResult(
        dive_id=payload["dive_id"], algorithm="a", algorithm_version="1",
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


async def _run(workflow_cls=AutomaticResultsForDiveWorkflow, *args, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        workers = [
            Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                   workflows=[AutomaticResultsForDiveWorkflow,
                              AutomaticResultsParentWorkflow]),
            Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                   activities=[_record],
                   workflows=[_Frames, _Species]),
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


async def test_one_dive_every_step_in_order():
    await _run(AutomaticResultsForDiveWorkflow, TARGET)

    assert EVENTS == [
        ("wake gpu",),
        ("stage",),
        ("child", f"automatic-frames-{DIVE}"),
        ("persist frames", 2),
        ("cleanup",),
        ("child", f"automatic-species-{DIVE}"),
        ("persist species", ["decode_failed"]),
        ("wake per-image",),
        ("child", f"automatic-calibration-{DIVE}"),
        ("persist calibration", "accepted", None),
        ("wake light",),
        ("child", f"automatic-measure-{DIVE}"),
        ("persist lengths", 1),
    ]


async def test_the_cpu_fallback_runs_no_gpu_step():
    await _run(AutomaticResultsForDiveWorkflow, TARGET, mode="cpu_fallback")
    kinds = [e[0] for e in EVENTS]
    assert "stage" not in kinds and "persist frames" not in kinds
    assert "persist species" not in kinds
    assert ("persist calibration", "accepted", None) in EVENTS
    assert ("persist lengths", 1) in EVENTS


async def test_no_gpu_work_wakes_no_gpu():
    await _run(AutomaticResultsForDiveWorkflow, TARGET, frames=0, species=0)
    assert ("wake gpu",) not in EVENTS and ("stage",) not in EVENTS


async def test_no_slate_frame_is_a_refusal_without_a_processor():
    await _run(AutomaticResultsForDiveWorkflow, TARGET, slate=0)
    assert ("persist calibration", "refused", "no_candidates") in EVENTS
    assert ("wake per-image",) not in EVENTS


async def test_no_calibration_means_no_lengths():
    await _run(AutomaticResultsForDiveWorkflow, TARGET, plan=None)
    assert ("wake light",) not in EVENTS


async def test_the_parent_runs_the_selected_dive():
    assert await _run(AutomaticResultsParentWorkflow) == TARGET
    assert ("persist lengths", 1) in EVENTS


async def test_nothing_selected_is_nothing_done():
    assert await _run(AutomaticResultsParentWorkflow, selected=None) is None
    assert EVENTS == []
