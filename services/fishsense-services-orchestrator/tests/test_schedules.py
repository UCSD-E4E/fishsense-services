"""The orchestrator's Temporal schedules.

Ported from fishsense-lite@a8b2c3bc: `schedule_workflow` / `ensure_schedule`
(fishsense_api_workflow_worker/worker.py, fishsense_shared/temporal.py) and the
stage-1 half of tests/test_schedule_registration.py. v1's rules, kept:

* a schedule is **created if missing and never updated in place** -- a config
  typo must not silently mutate or retire a production schedule; operators
  delete and redeploy to change one;
* a selector's schedule **skips on overlap**, so two firings never pick the
  same dive.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_orchestrator.clustering.workflow import (
    ClusterDiveFramesParentWorkflow,
)
from fishsense_services_orchestrator.schedules import SCHEDULES, ensure_schedules

QUEUE = "test-schedules"


async def _describe(client, schedule_id):
    return await client.get_schedule_handle(schedule_id).describe()


async def test_clustering_is_scheduled_hourly_at_5_and_skips_overlap():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        await ensure_schedules(env.client, task_queue=QUEUE)
        schedule = (await _describe(env.client, "cluster-dive-frames")).schedule

    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(hours=1)
    assert interval.offset == timedelta(minutes=5)
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.action.workflow == "ClusterDiveFramesParentWorkflow"
    assert schedule.action.task_queue == QUEUE
    assert schedule.action.run_timeout == timedelta(minutes=30)


async def test_an_existing_schedule_is_left_as_it_is():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        await ensure_schedules(env.client, task_queue="the-original-queue")
        await ensure_schedules(env.client, task_queue="a-typo")
        schedule = (await _describe(env.client, "cluster-dive-frames")).schedule

    assert schedule.action.task_queue == "the-original-queue"


def test_every_scheduled_workflow_is_one_the_worker_serves():
    from fishsense_services_orchestrator.worker import WORKFLOWS

    assert {s.workflow for s in SCHEDULES} <= set(WORKFLOWS)
    assert ClusterDiveFramesParentWorkflow in WORKFLOWS
