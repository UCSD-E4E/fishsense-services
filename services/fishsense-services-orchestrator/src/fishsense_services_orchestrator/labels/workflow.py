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
* the post-sync RANSAC validation pass ports with the processor's validation
  stage;
* **one project's failure doesn't cancel the others.** v1 ran them in a
  TaskGroup, so the first failure cancelled every other project mid-sync. Here
  each finishes on its own, and the workflow then fails naming the ones that
  didn't -- loud, without holding anyone else's labels back.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.labels.sync import LabelProject

__all__ = ["PROJECT_CONCURRENCY", "SyncLabelStudioLaserLabelsWorkflow"]

PROJECT_CONCURRENCY = 4


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
