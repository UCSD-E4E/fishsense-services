"""Workflow contract test for SyncLabelStudioLaserLabelsWorkflow.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_sync_label_studio_laser_labels_workflow.py (the sync half). Test names,
bodies and reasons are v1's; v2 adaptations: projects carry their tenant; there
is no user-sync step (v2 records Label Studio user ids directly -- PLAN.md
§9.14 maps them to people); the post-sync RANSAC validation pass ports with the
processor's validation stage.

v2 change, pinned last: **one project's failure doesn't cancel the others.**
v1 ran the projects in a TaskGroup, so the first failure cancelled every
other project mid-sync. Here each project syncs independently, and the
workflow then fails naming the ones that didn't.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import List

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.labels import workflow as sut
from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.labels.workflow import (
    SyncLabelStudioLaserLabelsWorkflow,
)

QUEUE = "test-laser-sync"
TENANT = uuid.uuid4()


def _stubs(projects, calls: List, *, fail=(), gate=None):
    @activity.defn(name="laser_label_projects")
    async def stub_projects() -> List[LabelProject]:
        calls.append("projects")
        return projects

    @activity.defn(name="sync_laser_labels")
    async def stub_sync(project: LabelProject) -> None:
        if gate is not None:
            await gate(project)
        if project.ls_project_id in fail:
            raise ApplicationError("simulated failure", non_retryable=True)
        calls.append(f"sync:{project.ls_project_id}")

    return [stub_projects, stub_sync]


async def _run(activities):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[SyncLabelStudioLaserLabelsWorkflow],
            activities=activities,
        ):
            return await env.client.execute_workflow(
                SyncLabelStudioLaserLabelsWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
                # A bare exception group escaping the workflow fails its task,
                # which Temporal retries forever (v1's warning): time out
                # rather than hang.
                execution_timeout=timedelta(seconds=30),
            )


async def test_workflow_invokes_project_ids_then_one_sync_per_project():
    calls: List[str] = []
    projects = [LabelProject(TENANT, 1), LabelProject(TENANT, 2)]

    await _run(_stubs(projects, calls))

    assert calls[0] == "projects"
    assert sorted(calls[1:]) == ["sync:1", "sync:2"]


async def test_workflow_with_no_projects_does_not_invoke_per_project_sync():
    calls: List[str] = []

    await _run(_stubs([], calls))

    assert calls == ["projects"]


async def test_per_project_activity_caps_concurrency_at_workflow_level():
    """Phase 1 regression guard: the workflow must not fan out one
    activity per project unbounded -- they share a `Semaphore(4)`."""
    calls: List[str] = []
    in_flight = 0
    peak = 0

    async def gate(_project):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1

    projects = [LabelProject(TENANT, i) for i in range(12)]

    await _run(_stubs(projects, calls, gate=gate))

    assert peak <= sut.PROJECT_CONCURRENCY
    assert len([c for c in calls if c.startswith("sync:")]) == 12


async def test_one_failing_project_does_not_cancel_the_others():
    """v2 fix: the others finish, and the workflow fails naming the one that
    didn't -- the failure is loud, and nobody else's labels wait on it."""
    calls: List[str] = []
    projects = [LabelProject(TENANT, i) for i in range(1, 5)]

    async def the_others_are_still_running(project):
        # The failure lands first; the others are mid-sync when it does.
        if project.ls_project_id != 2:
            await asyncio.sleep(0.3)

    with pytest.raises(WorkflowFailureError) as excinfo:
        await _run(_stubs(projects, calls, fail={2}, gate=the_others_are_still_running))

    assert sorted(calls[1:]) == ["sync:1", "sync:3", "sync:4"]
    assert "2" in str(excinfo.value.cause)
