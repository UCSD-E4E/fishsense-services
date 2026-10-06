"""The automatic-results workflows (orchestrator side).

New in v2. One backlog dive per run, every step of cscw-fishsense2027@96a8da07
PAPER.md §6's chain, each on the processor role that owns it:

1. **frames** (GPU): stage the raw frames, then per frame the detector's dot,
   the SAM 3.1 mask at it (score >= 0.5) and its head/tail; persist; clean up;
2. **species** (GPU): BioCLIP zero-shot on each kept mask, through the species
   stage's own processor workflow (`PredictSpeciesImagesWorkflow`);
3. **calibration** (per-image): the label-free size-constancy fit on the
   dive's slate frames; with none, the refusal is written without a processor;
4. **lengths** (light): at the automatic dot's depth, under the calibration
   the dive's lengths use now.

Rules kept from the other parents (`species_predict.workflow`,
`laser.workflow`): deterministic child ids reused ALLOW_DUPLICATE, and a child
still running under another firing means its step is skipped; the GPU is woken
only once there is GPU work; raw staging before the child and cleanup after,
the child named a raw-scratch reader. And one of its own: **only a real GPU
runs the GPU steps** -- the CPU fallback would run Mask R-CNN, whose lengths
were never validated, so on `cpu_fallback` or `unavailable` those steps wait
for a later run and the CPU steps go on with what is there.

`AutomaticResultsParentWorkflow` is the schedule's (the backlog's oldest
dive); `AutomaticResultsForDiveWorkflow` runs a named dive on demand, as the
validation harness does for the paper's dives, which are not in the backlog.
"""

from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_GPU_TASK_QUEUE,
        PROCESSOR_LIGHT_TASK_QUEUE,
        PROCESSOR_TASK_QUEUE,
    )
    from fishsense_services_contracts.automatic_results import (
        AUTOMATIC_CALIBRATION_VERSION,
        AutomaticCalibrationResult,
        AutomaticFrameResult,
        MeasureAutomaticResult,
        PredictAutomaticFramesInput,
    )
    from fishsense_services_contracts.species_prediction import (
        SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
        PredictSpeciesImagesInput,
        SpeciesPredictionResult,
    )
    from fishsense_services_orchestrator.automatic_results.activities import (
        AutomaticCalibrationPlan,
        AutomaticMeasurePlan,
        AutomaticTarget,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_GPU
    from fishsense_services_orchestrator.nrp.workflow import (
        GPU_WAKE_TIMEOUT,
        WAKE_TIMEOUT,
        wake_gpu_processor,
        wake_light_processor,
        wake_per_image_processor,
    )
    from fishsense_services_orchestrator.object_store.contracts import StagingTarget
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )

__all__ = [
    "AutomaticResultsForDiveWorkflow",
    "AutomaticResultsParentWorkflow",
    "RUN_TIMEOUT",
]

_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember", "InvalidPredictions"],
)
DB_TIMEOUT = timedelta(minutes=15)
GPU_CHILD_TIMEOUT = timedelta(hours=6)
CPU_CHILD_TIMEOUT = timedelta(hours=1)
STAGE_TIMEOUT = timedelta(hours=1)
#: The schedule's run timeout: every step at its longest (two GPU children,
#: two CPU ones, staging and cleanup, three wakes, nine database steps).
RUN_TIMEOUT = (
    9 * DB_TIMEOUT
    + GPU_WAKE_TIMEOUT
    + 2 * WAKE_TIMEOUT
    + 2 * GPU_CHILD_TIMEOUT
    + 2 * CPU_CHILD_TIMEOUT
    + 2 * STAGE_TIMEOUT
)


async def _db(name: str, *args, result_type=None):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=DB_TIMEOUT,
        retry_policy=_DB_FAIL_FAST,
        result_type=result_type,
    )


async def _child(name, arg, *, child_id, task_queue, timeout, result_type):
    """(ran, result): not ran when the same id is running under another firing,
    which then owns its step."""
    try:
        return True, await workflow.execute_child_workflow(
            name,
            arg,
            id=child_id,
            task_queue=task_queue,
            execution_timeout=timeout,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=result_type,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info("%s is running under another firing", child_id)
        return False, None


class _Gpu:
    """The GPU, woken at most once per run, and only when there is GPU work."""

    def __init__(self) -> None:
        self.mode: Optional[str] = None

    async def ready(self) -> bool:
        if self.mode is None:
            self.mode = await wake_gpu_processor()
        if self.mode != MODE_GPU:
            workflow.logger.warning(
                "automatic results need SAM 3.1 on a GPU; got %s, skipping the "
                "GPU steps this run",
                self.mode,
            )
        return self.mode == MODE_GPU


async def _frames(target: AutomaticTarget, gpu: _Gpu) -> None:
    inputs: PredictAutomaticFramesInput = await _db(
        "resolve_automatic_frames_inputs",
        target,
        result_type=PredictAutomaticFramesInput,
    )
    if not inputs.frames or not await gpu.ready():
        return
    staging = StagingTarget(target.tenant_id, target.dive_id)
    await stage_raw(staging)
    ran, results = await _child(
        "PredictAutomaticFramesWorkflow",
        inputs,
        child_id=raw_scratch_reader_id("automatic-frames", target.dive_id),
        task_queue=PROCESSOR_GPU_TASK_QUEUE,
        timeout=GPU_CHILD_TIMEOUT,
        result_type=List[AutomaticFrameResult],
    )
    if not ran:
        return
    if results:
        await _db("persist_automatic_frames", target, results, result_type=int)
    await cleanup_raw(staging)


async def _species(target: AutomaticTarget, gpu: _Gpu) -> None:
    inputs: PredictSpeciesImagesInput = await _db(
        "resolve_automatic_species_inputs",
        target,
        result_type=PredictSpeciesImagesInput,
    )
    if not inputs.images or not await gpu.ready():
        return
    ran, results = await _child(
        "PredictSpeciesImagesWorkflow",
        inputs,
        child_id=f"automatic-species-{target.dive_id}",
        task_queue=PROCESSOR_GPU_TASK_QUEUE,
        timeout=GPU_CHILD_TIMEOUT,
        result_type=List[SpeciesPredictionResult],
    )
    persistable = [
        r for r in results or [] if r.status != SPECIES_STATUS_NO_UPGRADE_AVAILABLE
    ]
    if ran and persistable:
        await _db("persist_automatic_species", target, persistable, result_type=int)


async def _calibration(target: AutomaticTarget) -> None:
    plan: AutomaticCalibrationPlan = await _db(
        "resolve_automatic_calibration_inputs",
        target,
        result_type=AutomaticCalibrationPlan,
    )
    if not plan.payload.frames:
        result = AutomaticCalibrationResult(
            dive_id=target.dive_id,
            outcome="refused",
            refusal_reason="no_candidates",
            algorithm_version=AUTOMATIC_CALIBRATION_VERSION,
        )
    else:
        await wake_per_image_processor()
        ran, result = await _child(
            "FitAutomaticCalibrationWorkflow",
            plan.payload,
            child_id=f"automatic-calibration-{target.dive_id}",
            task_queue=PROCESSOR_TASK_QUEUE,
            timeout=CPU_CHILD_TIMEOUT,
            result_type=AutomaticCalibrationResult,
        )
        if not ran:
            return
    await _db("persist_automatic_calibration", target, plan, result)


async def _lengths(target: AutomaticTarget) -> None:
    plan: Optional[AutomaticMeasurePlan] = await _db(
        "resolve_automatic_measure_inputs",
        target,
        result_type=Optional[AutomaticMeasurePlan],
    )
    if plan is None or not plan.payload.captures:
        return
    await wake_light_processor()
    ran, result = await _child(
        "MeasureAutomaticWorkflow",
        plan.payload,
        child_id=f"automatic-measure-{target.dive_id}",
        task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
        timeout=CPU_CHILD_TIMEOUT,
        result_type=MeasureAutomaticResult,
    )
    if ran:
        await _db(
            "persist_automatic_measurements", target, plan, result, result_type=int
        )


async def _run_for_dive(target: AutomaticTarget) -> None:
    gpu = _Gpu()
    await _frames(target, gpu)
    await _species(target, gpu)
    await _calibration(target)
    await _lengths(target)


@workflow.defn
class AutomaticResultsForDiveWorkflow:
    # pylint: disable=too-few-public-methods
    """Every automatic step for one named dive (on demand)::

        temporal workflow start --task-queue fishsense_orchestrator \\
            --type AutomaticResultsForDiveWorkflow \\
            --workflow-id automatic-results-<dive uuid> \\
            --input '{"tenant_id": "<uuid>", "dive_id": "<uuid>"}'
    """

    @workflow.run
    async def run(self, target: AutomaticTarget) -> AutomaticTarget:
        await _run_for_dive(target)
        return target


@workflow.defn
class AutomaticResultsParentWorkflow:
    # pylint: disable=too-few-public-methods
    """The backlog's oldest dive across tenants, every step. Returns it, or
    None when the backlog is empty."""

    @workflow.run
    async def run(self) -> Optional[AutomaticTarget]:
        target: Optional[AutomaticTarget] = await _db(
            "select_next_dive_for_automatic_results",
            result_type=Optional[AutomaticTarget],
        )
        if target is None:
            return None
        await _run_for_dive(target)
        return target
