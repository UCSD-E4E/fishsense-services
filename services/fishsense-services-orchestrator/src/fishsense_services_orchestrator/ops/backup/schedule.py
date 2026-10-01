"""The backup's Temporal schedule.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/src/
fishsense_backup_worker/schedule.py and `ensure_schedule`
(libs/fishsense-shared/src/fishsense_shared/temporal.py). v1's rules, kept: a
cron schedule (03:00 UTC), **created if missing and never updated in place** --
a config typo must not silently change the production schedule; operators
delete and redeploy to change it -- and Temporal's default overlap (skip),
stated.

v2 changes: the workflow id follows v2's schedules
(``{WorkflowClassName}-workflow``); the backup's schedule is its own process's,
not one of the orchestrator's stage schedules, because the orchestrator never
runs the backup.
"""

import logging
from typing import List

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

from fishsense_services_orchestrator.ops.backup.workflow import (
    BackupDatabasesInput,
    BackupDatabasesWorkflow,
)

__all__ = ["build_backup_schedule", "ensure_schedule"]

log = logging.getLogger(__name__)


def build_backup_schedule(
    *,
    databases: List[str],
    nas_root_path: str,
    retention_count: int,
    cron_expression: str,
    task_queue: str,
) -> Schedule:
    """The Schedule the backup process registers at startup."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            BackupDatabasesWorkflow.run,
            BackupDatabasesInput(
                databases=databases,
                nas_root_path=nas_root_path,
                retention_count=retention_count,
            ),
            id=f"{BackupDatabasesWorkflow.__name__}-workflow",
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(cron_expressions=[cron_expression]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_schedule(
    client: Client, *, schedule_id: str, schedule: Schedule
) -> None:
    """Create the schedule if it doesn't exist; leave an existing one alone."""
    try:
        await client.create_schedule(schedule_id, schedule)
        log.info("created schedule %s", schedule_id)
    except ScheduleAlreadyRunningError:
        log.info(
            "schedule %s already exists; leaving it as it is "
            "(delete and redeploy to change it)",
            schedule_id,
        )
