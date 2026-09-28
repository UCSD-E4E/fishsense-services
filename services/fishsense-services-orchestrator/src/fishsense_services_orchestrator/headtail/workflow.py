"""The head/tail workflows (orchestrator side).

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/: preprocess_headtail_images_parent_
workflow.py, predict_headtail_images_parent_workflow.py,
populate_headtail_label_studio_project_parent_workflow.py,
populate_headtail_label_studio_project_workflow.py (with _populate.py's
`create_then_populate` and its bounded retry),
create_headtail_label_studio_project_workflow.py,
backfill_headtail_predictions_workflow.py and
sync_label_studio_headtail_labels_workflow.py, and the dispatch rules of
_dispatch.py. The chain is v1's, hourly: +30 render (stage 5.1), +32 predict,
+34 populate -- populate is prediction-gated and decoupled from preprocess,
because it seeds rows and the predict cohort excludes an image with a live
label -- and the sync on the hour.

v1's rules, kept:

* a child's id is deterministic and reused ALLOW_DUPLICATE, never
  FAILED_ONLY; **a child still running means do nothing more** -- no cleanup,
  no flag clear, no persist (prod dive 442);
* stage 5.1 lowers only the flags of the frames it redrew, and the whole
  dive's when no work resolved (the flag is the one cohort term that never
  goes false on its own);
* predict needs no staging (it reads the stage-5.1 JPEG); `unavailable` from
  the GPU wake means return before dispatching (an unserved queue hangs);
  skips are never persisted; **the backfill runs unconditionally**, even for
  an all-skip run, because it is also what makes the predictions visible;
* populate fans out at most four dives at once, and one dive's failure does
  not fail the fan-out; its retries are bounded (5 attempts, 30 s apart and
  doubling), and a retry reconciles rather than re-imports.

v2 changes: the target is (tenant, dive); the children run on the processor's
queues, after the wakes stand it up (PLAN.md §3); staging and cleanup are the
object store's steps; the raw-reading child is named through
`raw_scratch_reader_id`; the predict parent no longer mistakes its
already-running sentinel for results (v1 iterated it); the sync has no
user-sync step, and one project's failure doesn't cancel the others. A
workflow's command order is its replay contract: these are new workflows,
and their order is v1's.
"""

import asyncio
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_GPU_TASK_QUEUE,
        PROCESSOR_TASK_QUEUE,
    )
    from fishsense_services_contracts.headtail import (
        HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
        HeadtailPredictionResult,
        PredictHeadtailImagesInput,
        PreprocessHeadtailImagesInput,
    )
    from fishsense_services_orchestrator.headtail.activities import (
        ClearHeadtailReprocessFlags,
        HeadtailTarget,
    )
    from fishsense_services_orchestrator.labels.sync import LabelProject
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_UNAVAILABLE
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_gpu_processor,
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
    "BackfillHeadtailPredictionsWorkflow",
    "CreateHeadTailLabelStudioProjectWorkflow",
    "PopulateHeadTailLabelStudioProjectParentWorkflow",
    "PopulateHeadTailLabelStudioProjectWorkflow",
    "PredictHeadtailImagesParentWorkflow",
    "PreprocessHeadtailImagesParentWorkflow",
    "SyncLabelStudioHeadTailLabelsWorkflow",
    "create_then_populate",
]

#: Every child is reused whatever its last run did: a completed id must not
#: stop a dive that gains work later from ever getting it done (dive 60).
CHILD_ID_REUSE = WorkflowIDReusePolicy.ALLOW_DUPLICATE

# Database round trips: one retry for a blip, then fail fast (v1's
# SDK_FAIL_FAST). A lost membership is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember"],
)

#: v1's `_POPULATE_RETRY`: bounded (unlimited let dive 424 reach attempt 10
#: and 23 copies of three frames), but starting at 30 s so a Label Studio blip
#: still has ~8 minutes of cover.
POPULATE_MAX_ATTEMPTS = 5
_POPULATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=POPULATE_MAX_ATTEMPTS,
    non_retryable_error_types=["NotAMember"],
)

#: Concurrent populate children (a handful of Label Studio calls each).
POPULATE_CONCURRENCY = 4
#: Concurrent project syncs.
PROJECT_CONCURRENCY = 4


# -- stage 5.1 ------------------------------------------------------------------------


@workflow.defn
class PreprocessHeadtailImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Render the next high-priority dive's head/tail JPEGs on the processor.
    Returns the target processed, or None when the cohort is empty."""

    @workflow.run
    async def run(self) -> Optional[HeadtailTarget]:
        target: Optional[HeadtailTarget] = await workflow.execute_activity(
            "select_next_dive_for_headtail_preprocessing",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[HeadtailTarget],
        )
        if target is None:
            return None

        inputs: PreprocessHeadtailImagesInput = await workflow.execute_activity(
            "resolve_headtail_preprocess_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=PreprocessHeadtailImagesInput,
        )
        workflow.logger.info(
            "dispatching headtail preprocess to the processor dive=%s images=%d",
            target.dive_id,
            len(inputs.images),
        )

        if not inputs.images:
            # A flag that reached no image must still come down, or the dive
            # is re-selected -- and re-staged from the NAS -- every hour.
            workflow.logger.warning(
                "reprocess flag resolved to no work; lowering it dive=%s",
                target.dive_id,
            )
            await _clear_flags(target, None)
            return target

        staging = StagingTarget(tenant_id=target.tenant_id, dive_id=target.dive_id)
        await wake_per_image_processor()
        await stage_raw(staging)
        try:
            await workflow.execute_child_workflow(
                "PreprocessHeadtailImagesWorkflow",
                inputs,
                id=raw_scratch_reader_id("preprocess-headtail", target.dive_id),
                task_queue=PROCESSOR_TASK_QUEUE,
                execution_timeout=timedelta(hours=1),
                id_reuse_policy=CHILD_ID_REUSE,
            )
        except WorkflowAlreadyStartedError:
            # Its owner is reading the scratch this would delete; it cleans
            # up and clears the flags of what it redrew.
            workflow.logger.info(
                "preprocess-headtail-%s is still running; leaving its raw bytes "
                "and reprocess flags alone",
                target.dive_id,
            )
            return target

        await cleanup_raw(staging)
        # Scoped to what this run redrew: a flag raised while the child ran
        # survives to the next firing.
        await _clear_flags(target, [image.checksum for image in inputs.images])
        return target


async def _clear_flags(target: HeadtailTarget, checksums: Optional[List[str]]) -> None:
    await workflow.execute_activity(
        "clear_headtail_reprocess_flags",
        ClearHeadtailReprocessFlags(
            tenant_id=target.tenant_id, dive_id=target.dive_id, checksums=checksums
        ),
        schedule_to_close_timeout=timedelta(minutes=15),
        retry_policy=_DB_FAIL_FAST,
    )


# -- predict ----------------------------------------------------------------------------


@workflow.defn
class PredictHeadtailImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Predict the next dive's head/tail keypoints on the processor's GPU
    queue, persist them, and attach them to its Label Studio tasks. Returns
    the target, or None when there was nothing to do or no worker."""

    @workflow.run
    async def run(self) -> Optional[HeadtailTarget]:
        target: Optional[HeadtailTarget] = await workflow.execute_activity(
            "select_next_dive_for_headtail_prediction",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[HeadtailTarget],
        )
        if target is None:
            return None

        inputs: PredictHeadtailImagesInput = await workflow.execute_activity(
            "resolve_headtail_predict_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=PredictHeadtailImagesInput,
        )
        workflow.logger.info(
            "dispatching headtail predict to the processor dive=%s images=%d",
            target.dive_id,
            len(inputs.images),
        )
        if not inputs.images:
            return target

        mode = await wake_gpu_processor()
        if mode == MODE_UNAVAILABLE:
            workflow.logger.warning(
                "no worker available for the headtail-predict queue; "
                "skipping dive=%s this firing",
                target.dive_id,
            )
            return None
        workflow.logger.info("headtail predict running on %s capacity", mode)

        try:
            results: List[HeadtailPredictionResult] = (
                await workflow.execute_child_workflow(
                    "PredictHeadtailImagesWorkflow",
                    inputs,
                    id=f"predict-headtail-{target.dive_id}",
                    task_queue=PROCESSOR_GPU_TASK_QUEUE,
                    # Generous enough for the CPU fallback, which runs far
                    # slower per image.
                    execution_timeout=timedelta(hours=6),
                    id_reuse_policy=CHILD_ID_REUSE,
                    result_type=List[HeadtailPredictionResult],
                )
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "predict-headtail-%s is still running; it persists and backfills",
                target.dive_id,
            )
            return target

        # A skip is about the worker, not the image: persisting it would
        # blank a good row.
        persistable = [
            r for r in results if r.status != HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE
        ]
        if persistable:
            await workflow.execute_activity(
                "persist_headtail_predictions",
                args=(target, persistable),
                schedule_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_attempts=2,
                    non_retryable_error_types=["InvalidPredictions", "NotAMember"],
                ),
            )

        # Unconditional, last, on purpose (v1, 2026-09-10): it also points the
        # project's model_version at the dive's tier, without which attached
        # predictions are invisible -- and an all-skip run is exactly the dive
        # that may need it.
        await workflow.execute_activity(
            "backfill_headtail_predictions_for_dive",
            target,
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=_DB_FAIL_FAST,
        )
        return target


# -- Label Studio ---------------------------------------------------------------------


async def create_then_populate(target: HeadtailTarget) -> int:
    """Materialise the dive's head/tail project, then push its tasks (v1's
    `_populate.create_then_populate`). Returns the rows recorded."""
    project_id = await workflow.execute_activity(
        "create_headtail_label_studio_project",
        target,
        schedule_to_close_timeout=timedelta(minutes=5),
    )
    return await workflow.execute_activity(
        "populate_headtail_label_studio_project",
        args=(target, project_id),
        schedule_to_close_timeout=timedelta(minutes=30),
        heartbeat_timeout=timedelta(minutes=2),
        retry_policy=_POPULATE_RETRY,
    )


@workflow.defn
class PopulateHeadTailLabelStudioProjectParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Fan populate out across every dive needing it. Returns the targets
    dispatched."""

    @workflow.run
    async def run(self) -> List[HeadtailTarget]:
        targets: List[HeadtailTarget] = await workflow.execute_activity(
            "select_dives_needing_headtail_population",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=List[HeadtailTarget],
        )
        if not targets:
            return []

        sem = asyncio.Semaphore(POPULATE_CONCURRENCY)

        async def _populate(target: HeadtailTarget) -> None:
            async with sem:
                try:
                    await workflow.execute_child_workflow(
                        "PopulateHeadTailLabelStudioProjectWorkflow",
                        target,
                        id=f"populate-headtail-{target.dive_id}",
                        execution_timeout=timedelta(minutes=30),
                        id_reuse_policy=CHILD_ID_REUSE,
                    )
                except WorkflowAlreadyStartedError:
                    workflow.logger.info(
                        "populate-headtail-%s already running; skipping duplicate "
                        "dispatch",
                        target.dive_id,
                    )

        # One dive's failure must not abort the fan-out.
        outcomes = await asyncio.gather(
            *(_populate(t) for t in targets), return_exceptions=True
        )
        for target, outcome in zip(targets, outcomes):
            if isinstance(outcome, BaseException):
                workflow.logger.error(
                    "populate-headtail-%s failed: %s", target.dive_id, outcome
                )
        return targets


@workflow.defn
class PopulateHeadTailLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """Create (or find) the dive's head/tail project, then populate it."""

    @workflow.run
    async def run(self, target: HeadtailTarget) -> int:
        return await create_then_populate(target)


@workflow.defn
class CreateHeadTailLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """Create (or find) the dive's head/tail project; on demand. Returns its
    Label Studio id."""

    @workflow.run
    async def run(self, target: HeadtailTarget) -> int:
        return await workflow.execute_activity(
            "create_headtail_label_studio_project",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
        )


@workflow.defn
class BackfillHeadtailPredictionsWorkflow:
    # pylint: disable=too-few-public-methods
    """Attach a dive's persisted predictions to its existing tasks; on demand:

        temporal workflow start --task-queue fishsense_orchestrator \\
            --type BackfillHeadtailPredictionsWorkflow \\
            --workflow-id backfill-headtail-predictions-<dive uuid> \\
            --input '{"tenant_id": "<uuid>", "dive_id": "<uuid>"}'

    Returns how many were attached; idempotent."""

    @workflow.run
    async def run(self, target: HeadtailTarget) -> int:
        return await workflow.execute_activity(
            "backfill_headtail_predictions_for_dive",
            target,
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=_DB_FAIL_FAST,
        )


@workflow.defn
class SyncLabelStudioHeadTailLabelsWorkflow:
    # pylint: disable=too-few-public-methods
    """Sync head/tail labels in from Label Studio, every project."""

    @workflow.run
    async def run(self) -> None:
        projects: List[LabelProject] = await workflow.execute_activity(
            "head_tail_label_projects",
            schedule_to_close_timeout=timedelta(minutes=10),
            result_type=List[LabelProject],
        )
        sem = asyncio.Semaphore(PROJECT_CONCURRENCY)

        async def _sync(project: LabelProject) -> None:
            async with sem:
                await workflow.execute_activity(
                    "sync_head_tail_labels",
                    project,
                    # Sized for a first run over a backlog project: the cursor
                    # is empty, so every task is paged (~7k pages at ~1s/page).
                    schedule_to_close_timeout=timedelta(hours=2),
                    heartbeat_timeout=timedelta(minutes=2),
                )

        outcomes = await asyncio.gather(
            *(_sync(project) for project in projects), return_exceptions=True
        )
        failed = [
            project.ls_project_id
            for project, outcome in zip(projects, outcomes)
            if isinstance(outcome, BaseException)
        ]
        if failed:
            raise ApplicationError(
                f"head/tail label sync failed for Label Studio project(s) {failed}; "
                f"the other {len(projects) - len(failed)} synced"
            )
