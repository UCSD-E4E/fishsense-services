"""The laser-depth and measure-fish schedules keep v1's.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_schedule_registration.py (these two schedules) and worker.py:688-741:
hourly, depth at :35 and measure at :40 (before calibration at :50, clear of
the :55 sweeper), a run timeout of 1 h 30, and skipping on overlap so two
firings never pick the same dive. v1's depth docstring said +25; its worker
said +35, and the worker is what ran.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_orchestrator.schedules import SCHEDULES, ensure_schedules

QUEUE = "test-schedules"


@pytest.mark.parametrize(
    "schedule_id, workflow, minute",
    [
        ("compute-laser-depths", "ComputeLaserDepthsParentWorkflow", 35),
        ("measure-fish", "MeasureFishParentWorkflow", 40),
    ],
)
async def test_scheduled_hourly_at_v1s_minute_and_skipping_overlap(
    schedule_id, workflow, minute
):
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        await ensure_schedules(env.client, task_queue=QUEUE)
        schedule = (
            await env.client.get_schedule_handle(schedule_id).describe()
        ).schedule

    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(hours=1)
    assert interval.offset == timedelta(minutes=minute)
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.action.workflow == workflow
    assert schedule.action.task_queue == QUEUE
    assert schedule.action.run_timeout == timedelta(hours=1, minutes=30)


def test_no_other_schedule_shares_their_minutes():
    """v1 spread its selectors over the hour so they never hit the database
    at once."""
    minutes = [s.offset for s in SCHEDULES if s.every == timedelta(hours=1)]
    for minute in (timedelta(minutes=35), timedelta(minutes=40)):
        assert minutes.count(minute) == 1
