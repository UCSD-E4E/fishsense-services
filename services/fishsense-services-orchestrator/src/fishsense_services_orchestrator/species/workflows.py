"""The species stages' orchestrator workflows.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/:
preprocess_species_images_parent_workflow.py (with the `_dispatch` steps it
uses), create_species_label_studio_project_workflow.py,
populate_species_label_studio_project_workflow.py (with `_populate.
create_then_populate` and its retry policy),
populate_species_label_studio_project_parent_workflow.py,
sync_label_studio_species_labels_workflow.py and
update_dive_image_groups_workflow.py. Timeouts, retry policies, concurrency
and child ids are v1's.

v1's invariants, kept:

* the stage-2 parent runs select -> resolve -> wake -> stage -> child ->
  cleanup -> clear; when nothing resolves it lowers the dive's flags
  whole-dive (a flag nothing lowers re-selects the dive hourly, forever), and
  on success only the frames it drew (a flag raised during the up-to-2 h
  child must survive);
* children are ALLOW_DUPLICATE, never FAILED_ONLY -- dedup lives in the
  activities -- and a firing that finds the child still running does
  nothing further: the run that owns it cleans up and clears (prod dive 442);
* populate is decoupled from preprocess, on its own +20 schedule;
* the populate activity's retries are bounded, and a retry reconciles the
  import rather than repeating it.

v2 changes:

* targets are (tenant, dive); the child runs on the processor's per-image
  queue under `raw_scratch_reader_id`'s id, and the wake stands that
  processor up (PLAN.md §3);
* the sync has no user-sync step (v2 keeps Label Studio ids; §9.14), and one
  project's failure doesn't cancel the others (as the laser sync's port);
* the flag clear names captures, not checksums.
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

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_contracts.species import PreprocessSpeciesImagesInput
    from fishsense_services_orchestrator.labels.sync import LabelProject
    from fishsense_services_orchestrator.nrp.workflow import wake_per_image_processor
    from fishsense_services_orchestrator.object_store.contracts import StagingTarget
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )
    from fishsense_services_orchestrator.species.contracts import (
        ClearReprocessFlagsInput,
        SpeciesTarget,
        UpdateDiveImageGroupsResult,
    )

__all__ = [
    "POPULATE_CONCURRENCY",
    "POPULATE_MAX_ATTEMPTS",
    "PROJECT_CONCURRENCY",
    "CreateSpeciesLabelStudioProjectWorkflow",
    "PopulateSpeciesLabelStudioProjectParentWorkflow",
    "PopulateSpeciesLabelStudioProjectWorkflow",
    "PreprocessSpeciesImagesParentWorkflow",
    "SyncLabelStudioSpeciesLabelsWorkflow",
    "UpdateDiveImageGroupsWorkflow",
    "create_then_populate",
]

#: The child that reads the dive's raw scratch (the cleanup gate knows it).
RAW_READER = "preprocess-species"

# Selector and resolver are database round trips: one retry for a transient
# blip, then fail (v1's SDK_FAIL_FAST). A lost membership is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember"],
)

#: Bounded on purpose: unlimited retries let dive 424 reach attempt 10 and
#: leave 23 copies of three frames. The intervals matter as much as the cap:
#: from 30 s, doubling to 5 min, five attempts ride out an ordinary Label
#: Studio blip. A retry reconciles the import (`labels.populate.IMPORT_ISSUED`)
#: rather than re-importing (v1's `_populate._POPULATE_RETRY`).
POPULATE_MAX_ATTEMPTS = 5
_POPULATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=POPULATE_MAX_ATTEMPTS,
    non_retryable_error_types=["NotAMember"],
)
_NOT_A_MEMBER_IS_FINAL = RetryPolicy(non_retryable_error_types=["NotAMember"])

#: Concurrent per-dive populate children: a large backlog must not hammer the
#: hosted Label Studio import endpoint (v1's).
POPULATE_CONCURRENCY = 4
#: Concurrent per-project syncs (v1's).
PROJECT_CONCURRENCY = 4


async def _clear_flags(payload: ClearReprocessFlagsInput) -> None:
    """v1's `run_sdk_activity`: 15 min, fail fast."""
    await workflow.execute_activity(
        "clear_species_reprocess_flags",
        payload,
        schedule_to_close_timeout=timedelta(minutes=15),
        retry_policy=_DB_FAIL_FAST,
    )


@workflow.defn
class PreprocessSpeciesImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest high-priority dive needing species preprocessing and
    dispatch its frames to the processor. Returns the target, or None when
    the cohort is empty."""

    @workflow.run
    async def run(self) -> Optional[SpeciesTarget]:
        target: Optional[SpeciesTarget] = await workflow.execute_activity(
            "select_next_dive_for_species_preprocessing",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[SpeciesTarget],
        )
        if target is None:
            return None

        inputs: PreprocessSpeciesImagesInput = await workflow.execute_activity(
            "resolve_species_preprocess_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=PreprocessSpeciesImagesInput,
        )
        drawn = [m.capture_id for group in inputs.cluster_members for m in group]
        workflow.logger.info(
            "dispatching species preprocess to the processor dive=%s clusters=%d "
            "images=%d",
            target.dive_id,
            len(inputs.cluster_members),
            len(drawn),
        )

        if not drawn:
            # A flag that reached no image still has to come down: it is the
            # one term of the cohort that does not go false on its own, so
            # leaving it up re-selects this dive every hour, re-staging its raw
            # frames and starving every dive behind it. Losing the operator's
            # request is the lesser harm, so it is lowered and logged.
            workflow.logger.warning(
                "reprocess flag resolved to no work; lowering it dive=%s",
                target.dive_id,
            )
            await _clear_flags(
                ClearReprocessFlagsInput(target.tenant_id, target.dive_id)
            )
            return target

        staging = StagingTarget(target.tenant_id, target.dive_id)
        await wake_per_image_processor()
        await stage_raw(staging)
        try:
            await workflow.execute_child_workflow(
                "PreprocessSpeciesImagesWorkflow",
                inputs,
                id=raw_scratch_reader_id(RAW_READER, target.dive_id),
                task_queue=PROCESSOR_TASK_QUEUE,
                execution_timeout=timedelta(hours=2),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            # Another run owns that child and is reading the raw scratch this
            # firing would delete. It will clean up, and it will clear the
            # flags for the frames it actually drew.
            workflow.logger.info(
                "dive=%s already has a child running; leaving its raw bytes and "
                "reprocess flags alone",
                target.dive_id,
            )
            return target

        await cleanup_raw(staging)
        # Scoped to what this run drew: a flag raised while the child ran
        # stays up for the next firing.
        await _clear_flags(
            ClearReprocessFlagsInput(target.tenant_id, target.dive_id, drawn)
        )
        return target


async def _create_project(target: SpeciesTarget) -> int:
    return await workflow.execute_activity(
        "create_species_label_studio_project",
        target,
        schedule_to_close_timeout=timedelta(minutes=5),
        retry_policy=_NOT_A_MEMBER_IS_FINAL,
        result_type=int,
    )


async def create_then_populate(target: SpeciesTarget) -> int:
    """Materialise the dive's species project, then push its tasks; the number
    of label rows written (v1's `_populate.create_then_populate("species")`)."""
    project_id = await _create_project(target)
    return await workflow.execute_activity(
        "populate_species_label_studio_project",
        args=(target, project_id),
        schedule_to_close_timeout=timedelta(minutes=30),
        heartbeat_timeout=timedelta(minutes=2),
        retry_policy=_POPULATE_RETRY,
        result_type=int,
    )


@workflow.defn
class CreateSpeciesLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """On demand: find or create the dive's species project; its id. Populate
    calls the same step itself, so this is rarely needed."""

    @workflow.run
    async def run(self, target: SpeciesTarget) -> int:
        return await _create_project(target)


@workflow.defn
class PopulateSpeciesLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """Create the dive's species project and push one task per frame that
    needs one. Returns the number of label rows written."""

    @workflow.run
    async def run(self, target: SpeciesTarget) -> int:
        return await create_then_populate(target)


@workflow.defn
class PopulateSpeciesLabelStudioProjectParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Fan species populate out across every dive needing it. Returns the
    targets it dispatched (empty when the cohort is)."""

    @workflow.run
    async def run(self) -> List[SpeciesTarget]:
        targets: List[SpeciesTarget] = await workflow.execute_activity(
            "select_dives_needing_species_population",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=List[SpeciesTarget],
        )
        if not targets:
            return []

        sem = asyncio.Semaphore(POPULATE_CONCURRENCY)

        async def _populate(target: SpeciesTarget) -> None:
            async with sem:
                try:
                    await workflow.execute_child_workflow(
                        "PopulateSpeciesLabelStudioProjectWorkflow",
                        target,
                        # Deterministic id dedupes against an overlapping
                        # firing; ALLOW_DUPLICATE (not FAILED_ONLY) so a later
                        # firing can populate newly laser-valid frames after
                        # this one closed -- the child is idempotent.
                        id=f"populate-species-{target.dive_id}",
                        execution_timeout=timedelta(minutes=30),
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    workflow.logger.info(
                        "populate-species-%s already running; skipping duplicate "
                        "dispatch",
                        target.dive_id,
                    )

        # One dive's populate failure must not abort the whole fan-out (v1's
        # suppress=True): each is logged, the rest carry on.
        outcomes = await asyncio.gather(
            *(_populate(target) for target in targets), return_exceptions=True
        )
        for target, outcome in zip(targets, outcomes):
            if isinstance(outcome, BaseException):
                workflow.logger.error(
                    "species populate failed for dive=%s: %s", target.dive_id, outcome
                )
        return targets


@workflow.defn
class SyncLabelStudioSpeciesLabelsWorkflow:
    # pylint: disable=too-few-public-methods
    """Sync species labels in from Label Studio, every project (stage 4.2)."""

    @workflow.run
    async def run(self) -> None:
        projects: List[LabelProject] = await workflow.execute_activity(
            "species_label_projects",
            schedule_to_close_timeout=timedelta(minutes=10),
            result_type=List[LabelProject],
        )

        sem = asyncio.Semaphore(PROJECT_CONCURRENCY)

        async def _sync(project: LabelProject) -> None:
            async with sem:
                await workflow.execute_activity(
                    "sync_species_labels",
                    project,
                    # Sized for the *first* run on a backlog project: the
                    # cursor is empty, so every task is paged.
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
                f"species label sync failed for Label Studio project(s) {failed}; "
                f"the other {len(projects) - len(failed)} synced"
            )


@workflow.defn
class UpdateDiveImageGroupsWorkflow:
    # pylint: disable=too-few-public-methods
    """On demand (stage 6.1), once a dive's species labeling is complete:
    regroup its prediction clusters into the label-studio clusters stage 14
    measures."""

    @workflow.run
    async def run(self, target: SpeciesTarget) -> UpdateDiveImageGroupsResult:
        return await workflow.execute_activity(
            "update_dive_image_groups",
            target,
            schedule_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=_NOT_A_MEMBER_IS_FINAL,
            result_type=UpdateDiveImageGroupsResult,
        )
