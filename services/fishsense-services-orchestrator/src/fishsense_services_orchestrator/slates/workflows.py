"""The slate stages' workflows: stage 9, the dive-slate project, its sync.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/ (preprocess_slate_images_parent_
workflow.py, create_dive_slate_label_studio_project_workflow.py,
populate_dive_slate_label_studio_project_workflow.py with _populate.py's
`create_then_populate` and `_POPULATE_RETRY`, sync_label_studio_dive_slate_
labels_workflow.py, and the _dispatch.py steps they used).

**Stage 9's parent** picks the oldest HIGH dive needing slate frames drawn,
and stages **two** inputs -- the raw `.ORF` frames and the slate template's
PDF -- because the processor composites the rendered template beside each
rectified frame. Then v1's play: wake, stage, child, clean up, populate, and
clear only the flags this run redrew. v1's rules, kept:

* a flag that resolved to no work is lowered for the whole dive, then the
  parent returns: the flag is the one cohort term that does not go false on
  its own, and left up it re-selects the dive hourly, ahead of every newer
  one (prod dive 60 held up 84/465/471 until 2026-08-04);
* a child already running under this dive's id (a manual run overlapping the
  schedule) owns the scratch and the flags: this firing touches neither
  (prod dive 442 lost 984 raw objects under a live child, 2026-09-07);
* the flags come down after populate, so a populate failure keeps them;
* children and populate reuse their ids with ALLOW_DUPLICATE: a *completed*
  one must not burn the id (a dive that later gained a frame never drained).

**Populate** creates the dive's project (idempotently) then pushes its tasks,
under v1's bounded retry (30 s, doubling, 5 minutes at most, 5 attempts); the
import itself reconciles rather than re-importing across attempts.

**The sync** pulls every live slate project's tasks in, four projects at a
time.

v2 changes:

* targets are (tenant, dive); the child runs on the processor's per-image
  queue under an id from `raw_scratch_reader_id`, so the cleanup gate knows
  it; the wake stands the per-image processor up (`nrp`);
* the resolver returns the processor's payload -- refs the orchestrator
  issued -- plus the checksums the flags are scoped to;
* **one project's sync failure doesn't cancel the others** (v1 ran them in a
  TaskGroup; the laser sync's port fixed the same), and there is no
  user-sync step: v2 records Label Studio user ids directly (PLAN.md §9.14).
"""

import asyncio
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from fishsense_services_orchestrator.labels.populate_policy import (
        CREATE_PROJECT_RETRY,
        POPULATE_RETRY,
    )
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_orchestrator.labels.sync import LabelProject
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_per_image_processor,
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
    from fishsense_services_orchestrator.slates.contracts import (
        ClearSlateFlagsInput,
        PopulateSlateProject,
        SlatePdfTarget,
        SlatePreprocessPlan,
    )

__all__ = [
    "CreateDiveSlateLabelStudioProjectWorkflow",
    "POPULATE_RETRY",
    "PROJECT_CONCURRENCY",
    "PopulateDiveSlateLabelStudioProjectWorkflow",
    "PreprocessSlateImagesParentWorkflow",
    "SyncLabelStudioDiveSlateLabelsWorkflow",
]

# Selector, resolver and flag writes are database round trips: one retry for a
# blip, then fail -- a consistent error is a bug (v1's SDK_FAIL_FAST). A lost
# membership, or a dive stage 9 cannot be resolved for, is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember", "SlateInputsUnavailable"],
)

#: Per-project syncs in flight at once (v1's).
PROJECT_CONCURRENCY = 4


async def _clear_flags(target: StagingTarget, checksums) -> int:
    return await workflow.execute_activity(
        "clear_slate_reprocess_flags",
        ClearSlateFlagsInput(
            tenant_id=target.tenant_id, dive_id=target.dive_id, checksums=checksums
        ),
        schedule_to_close_timeout=timedelta(minutes=15),
        retry_policy=_DB_FAIL_FAST,
        result_type=int,
    )


@workflow.defn
class PreprocessSlateImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest HIGH dive needing slate frames drawn, and draw them.
    Returns the target processed, or None when the cohort is empty."""

    @workflow.run
    async def run(self) -> Optional[StagingTarget]:
        target: Optional[StagingTarget] = await workflow.execute_activity(
            "select_next_dive_for_slate_preprocessing",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[StagingTarget],
        )
        if target is None:
            return None

        plan: SlatePreprocessPlan = await workflow.execute_activity(
            "resolve_slate_preprocess_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=SlatePreprocessPlan,
        )
        workflow.logger.info(
            "dispatching slate preprocess to the processor dive=%s images=%d slate=%s",
            target.dive_id,
            len(plan.payload.images),
            plan.payload.slate_template_id,
        )

        if not plan.payload.images:
            # A flag that reached no image still has to come down, or the dive
            # is re-selected every hour forever. Losing the operator's request
            # is the lesser harm, so it is lowered and logged.
            workflow.logger.warning(
                "reprocess flag resolved to no work; lowering it dive=%s",
                target.dive_id,
            )
            await _clear_flags(target, None)
            return target

        await wake_per_image_processor()
        await stage_raw(target)
        await workflow.execute_activity(
            "stage_slate_pdf",
            SlatePdfTarget(
                tenant_id=target.tenant_id,
                slate_template_id=plan.payload.slate_template_id,
            ),
            # v1's 5 minutes; the PDF is small, and it is HEAD-skipped once
            # staged.
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(non_retryable_error_types=["NotAMember"]),
            result_type=bool,
        )
        try:
            await workflow.execute_child_workflow(
                "PreprocessSlateImagesWorkflow",
                plan.payload,
                id=raw_scratch_reader_id("preprocess-slate", target.dive_id),
                task_queue=PROCESSOR_TASK_QUEUE,
                execution_timeout=timedelta(hours=1),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            # Another run owns that child and is reading the raw scratch this
            # firing would delete. It cleans up, and it clears the flags for
            # the frames it actually redrew.
            workflow.logger.info(
                "dive=%s already has a child running; leaving its raw bytes and "
                "reprocess flags alone",
                target.dive_id,
            )
            return target

        await cleanup_raw(target)
        try:
            await workflow.execute_child_workflow(
                "PopulateDiveSlateLabelStudioProjectWorkflow",
                target,
                id=f"populate-dive-slate-{target.dive_id}",
                execution_timeout=timedelta(minutes=30),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "populate-dive-slate-%s is still running; skipping duplicate "
                "dispatch",
                target.dive_id,
            )

        # Scoped to what this run redrew: the child can run for an hour, and
        # an unscoped clear would silently discard a flag raised meanwhile.
        await _clear_flags(target, plan.checksums)
        return target


@workflow.defn
class CreateDiveSlateLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """Idempotently create a dive's slate-labeling project; returns its id.
    Rarely needed on its own: populate calls the same activity first."""

    @workflow.run
    async def run(self, target: StagingTarget) -> int:
        return await workflow.execute_activity(
            "create_dive_slate_label_studio_project",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=CREATE_PROJECT_RETRY,
            result_type=int,
        )


@workflow.defn
class PopulateDiveSlateLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """Create the dive's slate project if need be, then push one task per
    still-unlabeled slate frame. Returns the number of label rows written."""

    @workflow.run
    async def run(self, target: StagingTarget) -> int:
        project_id: int = await workflow.execute_activity(
            "create_dive_slate_label_studio_project",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(non_retryable_error_types=["NotAMember"]),
            result_type=int,
        )
        return await workflow.execute_activity(
            "populate_dive_slate_label_studio_project",
            PopulateSlateProject(
                tenant_id=target.tenant_id,
                dive_id=target.dive_id,
                ls_project_id=project_id,
            ),
            schedule_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=POPULATE_RETRY,
            result_type=int,
        )


@workflow.defn
class SyncLabelStudioDiveSlateLabelsWorkflow:
    # pylint: disable=too-few-public-methods
    """Sync slate labels in from Label Studio, every project."""

    @workflow.run
    async def run(self) -> None:
        projects: List[LabelProject] = await workflow.execute_activity(
            "slate_label_projects",
            schedule_to_close_timeout=timedelta(minutes=10),
            result_type=List[LabelProject],
        )

        sem = asyncio.Semaphore(PROJECT_CONCURRENCY)

        async def _sync(project: LabelProject) -> None:
            async with sem:
                await workflow.execute_activity(
                    "sync_slate_labels",
                    project,
                    # Sized for the first run over a backlog project: the
                    # cursor is empty, so every task is paged (v1's 2 h).
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
                f"slate label sync failed for Label Studio project(s) {failed}; "
                f"the other {len(projects) - len(failed)} synced"
            )
