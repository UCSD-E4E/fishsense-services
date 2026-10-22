"""The shadow calibration's workflows (docs/shadow-size-constancy.md §3).

`ShadowCalibrationForMissionWorkflow` (also run by hand): resolve the
mission's slate frames; wake the GPU and segment them with SAM 3.1 (children
on the GPU queue, `SEGMENT_CHUNK` frames each, under the 2 MB payload limit,
in parallel); fit both dot sources and both variants on the light queue;
store the results in the shadow tables. Nothing it writes is read by
production. Review of #261:

* a slate frame with no render yet means the mission waits
  (``renders_pending``, nothing stored): a fit on part of its frames would be
  stored as final;
* a mission with no dot to segment wakes no GPU but is still fitted, so its
  refusals are stored and the cohort moves past it.

`ShadowCalibrationParentWorkflow` (hourly, when enabled): one mission after
another, calibrated missions first, until none is left or the drain window
has passed. A mission that fails is reported and passed over; only a GPU
that won't come up stops the drain.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_GPU_TASK_QUEUE,
        PROCESSOR_LIGHT_TASK_QUEUE,
    )
    from fishsense_services_contracts.shadow_calibration import (
        FitShadowCalibrationInput,
        ShadowFitResult,
        SlateSegment,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_GPU
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_gpu_processor,
        wake_light_processor,
    )
    from fishsense_services_orchestrator.shadow_calibration.activities import (
        ShadowPlan,
        ShadowTarget,
    )

__all__ = [
    "DRAIN_WINDOW",
    "MISSION_TIMEOUT",
    "SEGMENT_CHUNK",
    "ShadowCalibrationForMissionWorkflow",
    "ShadowCalibrationParentWorkflow",
]

#: Frames per SAM child: each carries a presigned render URL (~650 chars).
SEGMENT_CHUNK = 400
_DB = dict(schedule_to_close_timeout=timedelta(minutes=15),
           retry_policy=RetryPolicy(maximum_attempts=3,
                                    non_retryable_error_types=["NotAMember"]))  # fmt: skip
#: How long after its start a parent run may begin another mission.
DRAIN_WINDOW = timedelta(minutes=50)
#: One mission's run, bounded inside the parent's run timeout (the
#: schedule's: DRAIN_WINDOW + 12 h).
MISSION_TIMEOUT = timedelta(hours=11)
#: Each SAM child; the chunks run in parallel.
SEGMENT_TIMEOUT = timedelta(hours=6)


@workflow.defn(name="ShadowCalibrationForMissionWorkflow")
class ShadowCalibrationForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: ShadowTarget) -> str:
        plan: ShadowPlan = await workflow.execute_activity(
            "resolve_shadow_calibration_inputs", target, result_type=ShadowPlan, **_DB
        )
        if plan.unrendered:
            workflow.logger.info("shadow mission=%s: %d slate frames not rendered yet",
                                 target.mission_id, plan.unrendered)  # fmt: skip
            return "renders_pending"
        segments: List[SlateSegment] = []
        frames = plan.segment.frames
        if frames:
            if await wake_gpu_processor() != MODE_GPU:
                workflow.logger.warning("shadow mission=%s: no GPU; skipped",
                                        target.mission_id)  # fmt: skip
                return "no_gpu"
            chunks = await asyncio.gather(*[
                workflow.execute_child_workflow(
                    "SegmentSlateFramesWorkflow",
                    plan.segment.model_copy(
                        update={"frames": frames[at : at + SEGMENT_CHUNK]}),
                    id=f"shadow-segment-{target.mission_id}-{n}",
                    task_queue=PROCESSOR_GPU_TASK_QUEUE,
                    execution_timeout=SEGMENT_TIMEOUT,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=List[SlateSegment],
                )
                for n, at in enumerate(range(0, len(frames), SEGMENT_CHUNK))
            ])  # fmt: skip
            segments = [s for chunk in chunks for s in chunk]
        payload: FitShadowCalibrationInput = await workflow.execute_activity(
            "assemble_shadow_fit", args=(plan, segments),
            result_type=FitShadowCalibrationInput, **_DB,
        )  # fmt: skip
        await wake_light_processor()
        result: ShadowFitResult = await workflow.execute_child_workflow(
            "FitShadowCalibrationWorkflow",
            payload,
            id=f"shadow-fit-{target.mission_id}",
            task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
            execution_timeout=timedelta(hours=2),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=ShadowFitResult,
        )
        await workflow.execute_activity(
            "persist_shadow_calibration", args=(target, result), result_type=int, **_DB
        )
        return "stored"


@workflow.defn(name="ShadowCalibrationParentWorkflow")
class ShadowCalibrationParentWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self) -> List[str]:
        started = workflow.now()
        done: List[str] = []
        tried = []
        while workflow.now() - started < DRAIN_WINDOW:
            target: Optional[ShadowTarget] = await workflow.execute_activity(
                "select_next_mission_for_shadow_calibration", tried,
                result_type=Optional[ShadowTarget], **_DB,
            )  # fmt: skip
            if target is None:
                break
            tried.append(target.mission_id)
            try:
                outcome = await workflow.execute_child_workflow(
                    ShadowCalibrationForMissionWorkflow.run,
                    target,
                    id=f"shadow-calibration-{target.mission_id}",
                    execution_timeout=MISSION_TIMEOUT,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=str,
                )
            except ChildWorkflowError as exc:
                workflow.logger.warning("shadow mission=%s failed: %s",
                                        target.mission_id, exc.cause or exc)  # fmt: skip
                outcome = "failed"
            except WorkflowAlreadyStartedError:
                outcome = "running_elsewhere"  # a run by hand holds it
            done.append(f"{target.mission_id}:{outcome}")
            if outcome == "no_gpu":
                break  # every mission behind it would wait out the wake too
        return done
