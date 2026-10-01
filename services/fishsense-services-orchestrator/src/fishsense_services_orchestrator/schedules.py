"""The orchestrator's Temporal schedules.

Ported from fishsense-lite@a8b2c3bc: `schedule_workflow`
(fishsense_api_workflow_worker/worker.py) and `ensure_schedule`
(fishsense_shared/temporal.py). v1's rules:

* a schedule is **created if missing and never updated in place**: a config
  typo must not silently mutate or retire a production schedule. Operators
  delete and redeploy to change one;
* a selector's schedule **skips on overlap**, so two firings never pick the
  same dive;
* hourly schedules are **offset** across the hour, so their selectors don't all
  hit the database at once. Each keeps v1's minute.
"""

import logging

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    SchedulePolicy,
    ScheduleSpec,
)

from fishsense_services_orchestrator.registry import ScheduledWorkflow, stages

__all__ = ["SCHEDULES", "ScheduledWorkflow", "ensure_schedules"]

log = logging.getLogger(__name__)


#: Every stage's schedules (see `registry`).
SCHEDULES = tuple(schedule for stage in stages() for schedule in stage.schedules)


async def ensure_schedules(client: Client, *, task_queue: str) -> None:
    """Create every schedule that doesn't exist; leave the rest as they are."""
    for scheduled in SCHEDULES:
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                scheduled.workflow.run,
                id=f"{scheduled.workflow.__name__}-workflow",
                task_queue=task_queue,
                run_timeout=scheduled.run_timeout,
            ),
            spec=ScheduleSpec(
                intervals=[
                    ScheduleIntervalSpec(every=scheduled.every, offset=scheduled.offset)
                ]
            ),
            policy=SchedulePolicy(overlap=scheduled.overlap),
        )
        try:
            await client.create_schedule(scheduled.schedule_id, schedule)
            log.info("created schedule %s", scheduled.schedule_id)
        except ScheduleAlreadyRunningError:
            log.info(
                "schedule %s already exists; leaving it as it is "
                "(delete and redeploy to change it)",
                scheduled.schedule_id,
            )
