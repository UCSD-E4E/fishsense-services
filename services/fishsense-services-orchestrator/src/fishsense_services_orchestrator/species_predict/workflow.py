"""The species pre-annotation workflows (orchestrator side).

**New in v2; v1 has no counterpart.** Shaped as head/tail prediction's parent
is (`headtail.workflow.PredictHeadtailImagesParentWorkflow`), and keeps its
rules:

* the child's id is deterministic and reused ALLOW_DUPLICATE; **a child still
  running means do nothing more** (its owner persists and backfills);
* nothing to stage: the child reads the head/tail stage's JPEG, already in
  Garage; `unavailable` from the GPU wake means return before dispatching
  (an unserved queue hangs);
* skips are never persisted; **the backfill runs unconditionally**, even for
  an all-skip run, because it is also what makes suggestions visible (and it
  attaches nothing while the stage is disabled);
* the run timeout covers every step at its longest, so a slow CPU-fallback
  child is not terminated with its parent.

`BackfillSpeciesPredictionsWorkflow` is the backfill on demand, as head/tail's.
"""

from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
    from fishsense_services_contracts.species_prediction import (
        SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
        PredictSpeciesImagesInput,
        SpeciesPredictionResult,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_UNAVAILABLE
    from fishsense_services_orchestrator.nrp.workflow import (
        GPU_WAKE_TIMEOUT,
        wake_gpu_processor,
    )
    from fishsense_services_orchestrator.species_predict.activities import (
        SpeciesPredictTarget,
    )

__all__ = [
    "BackfillSpeciesPredictionsWorkflow",
    "GPU_WAKE_TIMEOUT",
    "PREDICT_RUN_TIMEOUT",
    "PredictSpeciesImagesParentWorkflow",
]

CHILD_ID_REUSE = WorkflowIDReusePolicy.ALLOW_DUPLICATE

# Database round trips: one retry for a blip, then fail fast. A lost
# membership is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember"],
)

#: The parent's steps, each the most it can take (retries included).
PREDICT_SELECT_TIMEOUT = timedelta(minutes=5)
PREDICT_RESOLVE_TIMEOUT = timedelta(minutes=5)
#: Generous enough for the CPU fallback, which runs ViT-H/14 far slower.
PREDICT_CHILD_TIMEOUT = timedelta(hours=6)
PREDICT_PERSIST_TIMEOUT = timedelta(minutes=15)
PREDICT_BACKFILL_TIMEOUT = timedelta(minutes=15)
#: The schedule's run timeout: every step at its longest.
PREDICT_RUN_TIMEOUT = (
    PREDICT_SELECT_TIMEOUT
    + PREDICT_RESOLVE_TIMEOUT
    + GPU_WAKE_TIMEOUT
    + PREDICT_CHILD_TIMEOUT
    + PREDICT_PERSIST_TIMEOUT
    + PREDICT_BACKFILL_TIMEOUT
)


@workflow.defn
class PredictSpeciesImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Classify the next dive's fish with BioCLIP on the processor's GPU
    queue, persist the predictions, and attach them to its species tasks as
    suggestions (when enabled). Returns the target, or None when there was
    nothing to do or no worker."""

    @workflow.run
    async def run(self) -> Optional[SpeciesPredictTarget]:
        target: Optional[SpeciesPredictTarget] = await workflow.execute_activity(
            "select_next_dive_for_species_prediction",
            schedule_to_close_timeout=PREDICT_SELECT_TIMEOUT,
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[SpeciesPredictTarget],
        )
        if target is None:
            return None

        inputs: PredictSpeciesImagesInput = await workflow.execute_activity(
            "resolve_species_predict_inputs",
            target,
            schedule_to_close_timeout=PREDICT_RESOLVE_TIMEOUT,
            retry_policy=_DB_FAIL_FAST,
            result_type=PredictSpeciesImagesInput,
        )
        workflow.logger.info(
            "dispatching species predict to the processor dive=%s images=%d",
            target.dive_id,
            len(inputs.images),
        )
        if not inputs.images:
            return target

        mode = await wake_gpu_processor()
        if mode == MODE_UNAVAILABLE:
            workflow.logger.warning(
                "no worker available for the species-predict queue; "
                "skipping dive=%s this firing",
                target.dive_id,
            )
            return None
        workflow.logger.info("species predict running on %s capacity", mode)

        try:
            results: List[SpeciesPredictionResult] = (
                await workflow.execute_child_workflow(
                    "PredictSpeciesImagesWorkflow",
                    inputs,
                    id=f"predict-species-{target.dive_id}",
                    task_queue=PROCESSOR_GPU_TASK_QUEUE,
                    execution_timeout=PREDICT_CHILD_TIMEOUT,
                    id_reuse_policy=CHILD_ID_REUSE,
                    result_type=List[SpeciesPredictionResult],
                )
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "predict-species-%s is still running; it persists and backfills",
                target.dive_id,
            )
            return target

        # A skip is about the worker, not the fish: persisting it would
        # blank a good row.
        persistable = [
            r for r in results if r.status != SPECIES_STATUS_NO_UPGRADE_AVAILABLE
        ]
        if persistable:
            await workflow.execute_activity(
                "persist_species_predictions",
                args=(target, persistable),
                schedule_to_close_timeout=PREDICT_PERSIST_TIMEOUT,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_attempts=2,
                    non_retryable_error_types=["InvalidPredictions", "NotAMember"],
                ),
            )

        # Unconditional, last: it also points the project's model_version at
        # the suggestions, without which attached predictions are invisible.
        await workflow.execute_activity(
            "backfill_species_predictions_for_dive",
            target,
            schedule_to_close_timeout=PREDICT_BACKFILL_TIMEOUT,
            retry_policy=_DB_FAIL_FAST,
        )
        return target


@workflow.defn
class BackfillSpeciesPredictionsWorkflow:
    # pylint: disable=too-few-public-methods
    """Attach a dive's persisted species predictions to its existing species
    tasks as suggestions; on demand (a no-op while the stage is disabled):

        temporal workflow start --task-queue fishsense_orchestrator \\
            --type BackfillSpeciesPredictionsWorkflow \\
            --workflow-id backfill-species-predictions-<dive uuid> \\
            --input '{"tenant_id": "<uuid>", "dive_id": "<uuid>"}'

    Returns how many were attached; idempotent."""

    @workflow.run
    async def run(self, target: SpeciesPredictTarget) -> int:
        return await workflow.execute_activity(
            "backfill_species_predictions_for_dive",
            target,
            schedule_to_close_timeout=PREDICT_BACKFILL_TIMEOUT,
            retry_policy=_DB_FAIL_FAST,
        )
