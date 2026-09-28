"""Sync laser labels in from Label Studio, every project.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/sync_label_studio_laser_labels_workflow.py
(the sync half). Per-project syncs run at most `PROJECT_CONCURRENCY` at a
time, each sized for a first run over a backlog project (the cursor starts
empty, so every task is paged).

v2 changes:

* projects carry their tenant;
* no user-sync step: v2 records Label Studio user ids directly, and mapping
  them to people is PLAN.md §9.14;
* **one project's failure doesn't cancel the others.** v1 ran them in a
  TaskGroup, so the first failure cancelled every other project mid-sync. Here
  each finishes on its own, and the workflow then fails naming the ones that
  didn't -- loud, without holding anyone else's labels back.

The post-sync validation pass is v1's too (fishsense-lite@77e8f8e5, the same
file's second half): once the syncs land, every dive whose laser labeling is
complete is validated -- the light processor woken first, and only when there
is a dive -- at most `VALIDATION_CONCURRENCY` at a time, under v1's ids
`validate-laser-labels-{dive}` (ALLOW_DUPLICATE: an hourly re-run is cheap),
with failures logged and suppressed so a failed validation never rolls back a
successful sync. As in v1 it runs only after every project synced. v2 change:
each dive is an orchestrator child (`laser.workflow.
ValidateDiveLaserLabelsWorkflow`) that reads, has the light processor judge,
and writes -- the processor never touches the database.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.labels.sync import LabelProject
    from fishsense_services_orchestrator.laser.contracts import LaserTarget
    from fishsense_services_orchestrator.nrp.workflow import wake_light_processor

__all__ = [
    "PROJECT_CONCURRENCY",
    "VALIDATION_CONCURRENCY",
    "SyncLabelStudioLaserLabelsWorkflow",
]

PROJECT_CONCURRENCY = 4

#: v1's bound on concurrent per-dive validations.
VALIDATION_CONCURRENCY = 8


@workflow.defn
class SyncLabelStudioLaserLabelsWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self) -> None:
        projects: List[LabelProject] = await workflow.execute_activity(
            "laser_label_projects",
            schedule_to_close_timeout=timedelta(minutes=10),
            result_type=List[LabelProject],
        )

        sem = asyncio.Semaphore(PROJECT_CONCURRENCY)

        async def _sync(project: LabelProject) -> None:
            async with sem:
                await workflow.execute_activity(
                    "sync_laser_labels",
                    project,
                    # Sized for the *first* run on a backlog project -- the
                    # cursor is empty, so every task is paged even if the
                    # project is dormant. Later runs return almost at once.
                    # ~7k Label Studio pages fit in 2h at ~1s/page.
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
                f"laser label sync failed for Label Studio project(s) {failed}; "
                f"the other {len(projects) - len(failed)} synced"
            )

        await _validate_complete_dives()


async def _validate_complete_dives() -> None:
    """The post-sync validation pass (see the module docstring)."""
    dives: List[LaserTarget] = await workflow.execute_activity(
        "laser_dives_with_complete_labeling",
        schedule_to_close_timeout=timedelta(minutes=10),
        result_type=List[LaserTarget],
    )
    if not dives:
        return
    # The children judge on the light queue, which nothing polls until the
    # processor is stood up; v1's children once sat unpolled for 20 minutes.
    await wake_light_processor()
    sem = asyncio.Semaphore(VALIDATION_CONCURRENCY)

    async def _validate(target: LaserTarget) -> None:
        async with sem:
            try:
                await workflow.execute_child_workflow(
                    "ValidateDiveLaserLabelsWorkflow",
                    target,
                    id=f"validate-laser-labels-{target.dive_id}",
                    # The read, the processor's 20 minutes, and the write.
                    execution_timeout=timedelta(minutes=30),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                workflow.logger.info(
                    "validate-laser-labels-%s already running; skipping",
                    target.dive_id,
                )

    outcomes = await asyncio.gather(
        *(_validate(t) for t in dives), return_exceptions=True
    )
    for target, outcome in zip(dives, outcomes):
        if isinstance(outcome, BaseException):
            workflow.logger.error(
                "laser label validation failed for dive=%s: %s",
                target.dive_id,
                outcome,
            )
