"""The slate detector's parent workflow (orchestrator side).

**New in v2.** The model is 2026-10-03_slate_detector@95a77d95's presence
classifier, which reads the whole rectified frame, so the parent is shaped as
laser prediction's is (`laser.workflow.PredictLaserImagesParentWorkflow`: a
raw-reading child on the GPU queue), and keeps its rules:

* per dive: select, resolve, wake the GPU processor, stage the raws,
  dispatch the child, persist, clean the scratch up;
* **unlike laser prediction, a run drains the backlog**: every canonical frame
  is scored once, so it takes the next dive until `DETECT_DRAIN_WINDOW` has
  passed since it started, the cohort is empty, or it hands back a dive this
  run already took (one whose frames could not be persisted would otherwise
  spin), and it tells the cohort which dives it took;
* a dive with a raw the NAS no longer has (moved since ingest) is cleaned up
  and skipped, logged at ERROR: it must not hold the backlog;
* nothing to detect needs no worker; `unavailable` from the GPU wake ends
  the run **before staging** (a child on an unserved queue hangs until its
  execution timeout); the dive stays in the cohort;
* the child's id comes from `raw_scratch_reader_id("detect-slate", dive)`, so
  another stage's cleanup waits for it, and it is reused ALLOW_DUPLICATE; **a
  child still running means do nothing more** -- it owns the scratch, and its
  own parent persists;
* the run timeout covers the window and then one dive's every step at its
  longest, so a slow CPU-fallback child is not terminated with its parent.
"""

import uuid
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
    from fishsense_services_orchestrator.ingest.nas_errors import (
        NAS_FILE_NOT_FOUND_TYPE,
    )
    from fishsense_services_contracts.slate_presence import (
        DetectSlateImagesInput,
        SlatePresenceResult,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_UNAVAILABLE
    from fishsense_services_orchestrator.nrp.workflow import (
        GPU_WAKE_TIMEOUT,
        wake_gpu_processor,
    )
    from fishsense_services_orchestrator.object_store.contracts import (
        StagingTarget,
    )
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )

__all__ = [
    "CLEANUP_RAW_TIMEOUT",
    "DETECT_CHILD_TIMEOUT",
    "DETECT_DRAIN_WINDOW",
    "DETECT_PERSIST_TIMEOUT",
    "DETECT_RESOLVE_TIMEOUT",
    "DETECT_RUN_TIMEOUT",
    "DETECT_SELECT_TIMEOUT",
    "DetectSlatePresenceParentWorkflow",
    "GPU_WAKE_TIMEOUT",
    "STAGE_RAW_TIMEOUT",
]

# Database round trips: one retry for a blip, then fail fast. A lost
# membership, or a dive that cannot be resolved, is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember", "SlateDetectionUnavailable"],
)

#: The parent's steps, each the most it can take (retries included).
DETECT_SELECT_TIMEOUT = timedelta(minutes=5)
DETECT_RESOLVE_TIMEOUT = timedelta(minutes=5)
#: `object_store.steps`' budgets.
STAGE_RAW_TIMEOUT = timedelta(hours=1)
CLEANUP_RAW_TIMEOUT = timedelta(minutes=15)
#: A frame is ~20 s, almost all of it the raw decode, at 2 at a time; a
#: 1,000-frame dive is under 3 h on either the GPU or its CPU fallback.
DETECT_CHILD_TIMEOUT = timedelta(hours=6)
DETECT_PERSIST_TIMEOUT = timedelta(minutes=15)
#: How long after its start a run may begin another dive. Under the hourly
#: schedule (overlaps skipped), a dive is ~8 minutes, so ~6 dives an hour.
DETECT_DRAIN_WINDOW = timedelta(minutes=50)
#: The schedule's run timeout: the window, then one dive's every step at its
#: longest.
DETECT_RUN_TIMEOUT = (
    DETECT_DRAIN_WINDOW
    + DETECT_SELECT_TIMEOUT
    + DETECT_RESOLVE_TIMEOUT
    + GPU_WAKE_TIMEOUT
    + STAGE_RAW_TIMEOUT
    + DETECT_CHILD_TIMEOUT
    + DETECT_PERSIST_TIMEOUT
    + CLEANUP_RAW_TIMEOUT
)


#: What one dive's detection tells the drain.
_NEXT = "next"
_SKIPPED = "skipped"
_STOP = "stop"


@workflow.defn
class DetectSlatePresenceParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Score the backlog's unpredicted frames for a slate, dive after dive,
    on the processor's GPU queue, and persist the predictions. Returns the
    last dive taken, or None when there was nothing to do or no worker."""

    @workflow.run
    async def run(self) -> Optional[StagingTarget]:
        started = workflow.now()
        taken: List[uuid.UUID] = []
        last: Optional[StagingTarget] = None
        while workflow.now() - started < DETECT_DRAIN_WINDOW:
            target: Optional[StagingTarget] = await workflow.execute_activity(
                "select_next_dive_for_slate_detection",
                list(taken),
                schedule_to_close_timeout=DETECT_SELECT_TIMEOUT,
                retry_policy=_DB_FAIL_FAST,
                result_type=Optional[StagingTarget],
            )
            if target is None or target.dive_id in taken:
                break
            taken.append(target.dive_id)
            outcome = await self._detect(target)
            if outcome is None:
                break
            if outcome == _SKIPPED:
                continue
            last = target
            if outcome == _STOP:
                break
        return last

    @staticmethod
    async def _detect(target: StagingTarget) -> Optional[str]:
        """One dive. None: no worker, so nothing was done."""
        inputs: DetectSlateImagesInput = await workflow.execute_activity(
            "resolve_slate_detection_inputs",
            target,
            schedule_to_close_timeout=DETECT_RESOLVE_TIMEOUT,
            retry_policy=_DB_FAIL_FAST,
            result_type=DetectSlateImagesInput,
        )
        workflow.logger.info(
            "dispatching slate detection dive=%s images=%d",
            target.dive_id,
            len(inputs.images),
        )
        if not inputs.images:
            return _NEXT

        mode = await wake_gpu_processor()
        if mode == MODE_UNAVAILABLE:
            workflow.logger.warning(
                "no worker available for the slate-detect queue; skipping dive=%s",
                target.dive_id,
            )
            return None
        workflow.logger.info("slate detection running on %s capacity", mode)

        try:
            await stage_raw(target)
        except ActivityError as exc:
            cause = exc.cause
            if not (
                isinstance(cause, ApplicationError)
                and cause.type == NAS_FILE_NOT_FOUND_TYPE
            ):
                raise
            workflow.logger.error(
                "skipping dive=%s: a raw is missing from the NAS (%s)",
                target.dive_id,
                cause.message,
            )
            await cleanup_raw(target)
            return _SKIPPED
        try:
            results: List[SlatePresenceResult] = await workflow.execute_child_workflow(
                "DetectSlatePresenceWorkflow",
                inputs,
                id=raw_scratch_reader_id("detect-slate", target.dive_id),
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                execution_timeout=DETECT_CHILD_TIMEOUT,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[SlatePresenceResult],
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "detect-slate-%s is still running under another firing; it owns "
                "the scratch and persists",
                target.dive_id,
            )
            return _STOP

        if results:
            await workflow.execute_activity(
                "persist_slate_presence_predictions",
                args=(target, results),
                schedule_to_close_timeout=DETECT_PERSIST_TIMEOUT,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_attempts=2,
                    non_retryable_error_types=["InvalidPredictions", "NotAMember"],
                ),
            )
        await cleanup_raw(target)
        return _NEXT
