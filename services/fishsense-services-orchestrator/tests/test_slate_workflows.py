"""The dive-slate project's populate workflow and the slate sync workflow.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_populate_workflow_retry_policy.py and
test_populate_label_studio_project_workflows.py (the dive-slate variant), and
test_sync_label_studio_dive_slate_labels_workflow.py. Names and reasons are
v1's.

v2 changes, pinned here:

* the targets carry their tenant; the populate activity takes the created
  project with its (tenant, dive);
* the sync lists projects with their tenants and has **no user-sync step**
  (v2 records Label Studio user ids directly, PLAN.md §9.14);
* **one project's sync failure doesn't cancel the others**: v1 ran them in a
  TaskGroup, so the first failure cancelled every other project mid-sync.
  Here each finishes, and the workflow then fails naming the ones that
  didn't (the laser sync's port, for the same reason).
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
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.slates.contracts import PopulateSlateProject
from fishsense_services_orchestrator.slates.workflows import (
    POPULATE_RETRY,
    PROJECT_CONCURRENCY,
    CreateDiveSlateLabelStudioProjectWorkflow,
    PopulateDiveSlateLabelStudioProjectWorkflow,
    SyncLabelStudioDiveSlateLabelsWorkflow,
)

TENANT = uuid.UUID(int=1)
TARGET = StagingTarget(tenant_id=TENANT, dive_id=uuid.UUID(int=393))
QUEUE = "test-slate-workflows"


async def _execute(workflow_cls, arg, activities, *, max_concurrent=None):
    kwargs = (
        {} if max_concurrent is None else {"max_concurrent_activities": max_concurrent}
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[workflow_cls],
            activities=activities,
            **kwargs,
        ):
            args = () if arg is None else (arg,)
            return await env.client.execute_workflow(
                workflow_cls.run,
                *args,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


# ---------- populate ----------


def _populate_stubs(calls):
    @activity.defn(name="create_dive_slate_label_studio_project")
    async def create(target: StagingTarget) -> int:
        calls.append(("create", target, activity.info()))
        return 7

    @activity.defn(name="populate_dive_slate_label_studio_project")
    async def populate(payload: PopulateSlateProject) -> int:
        calls.append(("populate", payload, activity.info()))
        return 42

    return [create, populate]


async def test_emits_create_then_populate_in_order():
    calls: list = []

    result = await _execute(
        PopulateDiveSlateLabelStudioProjectWorkflow, TARGET, _populate_stubs(calls)
    )

    assert [c[0] for c in calls] == ["create", "populate"]
    assert calls[0][1] == TARGET
    assert calls[1][1] == PopulateSlateProject(
        tenant_id=TENANT, dive_id=TARGET.dive_id, ls_project_id=7
    ), "populate takes the created project id"
    assert result == 42


async def test_timeouts_are_v1s():
    calls: list = []

    await _execute(
        PopulateDiveSlateLabelStudioProjectWorkflow, TARGET, _populate_stubs(calls)
    )

    create_info, populate_info = calls[0][2], calls[1][2]
    assert create_info.schedule_to_close_timeout == timedelta(minutes=5)
    assert populate_info.schedule_to_close_timeout == timedelta(minutes=30)
    assert populate_info.heartbeat_timeout == timedelta(minutes=2)


def test_populate_retries_are_bounded():
    """Unlimited retries are what compounded duplicates (dive 424: attempt 10
    over 1107 s, 23 copies of three frames)."""
    assert 1 < POPULATE_RETRY.maximum_attempts <= 5


def test_the_retry_window_still_absorbs_a_label_studio_blip():
    """Capping attempts alone would have been a regression: a half-minute LS
    blip would fail the populate child and, through it, the stage-9 parent --
    before it clears its flags, so the dive would re-stage from the NAS."""
    policy = POPULATE_RETRY
    assert policy.initial_interval >= timedelta(seconds=30)
    assert policy.maximum_interval is not None

    window = timedelta()
    interval = policy.initial_interval
    for _ in range(policy.maximum_attempts - 1):
        window += interval
        interval = min(interval * policy.backoff_coefficient, policy.maximum_interval)
    assert window >= timedelta(minutes=5), "must ride out an ordinary LS blip"
    assert window <= timedelta(minutes=15), "but stay far short of the old 30"


async def test_the_create_workflow_returns_the_project():
    calls: list = []

    project = await _execute(
        CreateDiveSlateLabelStudioProjectWorkflow, TARGET, _populate_stubs(calls)
    )

    assert project == 7
    assert [c[0] for c in calls] == ["create"]


# ---------- the sync ----------


async def test_workflow_invokes_project_ids_then_one_sync_per_project():
    calls: List[tuple] = []
    projects = [LabelProject(TENANT, p) for p in (66, 67, 68)]

    @activity.defn(name="slate_label_projects")
    async def stub_projects() -> List[LabelProject]:
        calls.append(("projects",))
        return projects

    @activity.defn(name="sync_slate_labels")
    async def stub_sync(project: LabelProject) -> None:
        calls.append(("sync", project.ls_project_id))

    await _execute(
        SyncLabelStudioDiveSlateLabelsWorkflow, None, [stub_projects, stub_sync]
    )

    assert calls[0] == ("projects",)
    assert {c[1] for c in calls[1:]} == {66, 67, 68}


async def test_workflow_with_no_projects_does_not_invoke_per_project_sync():
    synced: List[int] = []

    @activity.defn(name="slate_label_projects")
    async def stub_projects() -> List[LabelProject]:
        return []

    @activity.defn(name="sync_slate_labels")
    async def stub_sync(project: LabelProject) -> None:
        synced.append(project.ls_project_id)

    await _execute(
        SyncLabelStudioDiveSlateLabelsWorkflow, None, [stub_projects, stub_sync]
    )

    assert not synced


async def test_per_project_activity_caps_concurrency_at_workflow_level():
    """Workflow-level cap mirrors laser/headtail: PROJECT_CONCURRENCY=4."""
    in_flight = 0
    peak = 0
    started: List[int] = []

    @activity.defn(name="slate_label_projects")
    async def stub_projects() -> List[LabelProject]:
        return [LabelProject(TENANT, p) for p in range(20)]

    @activity.defn(name="sync_slate_labels")
    async def stub_sync(project: LabelProject) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        started.append(project.ls_project_id)
        try:
            await asyncio.sleep(0.05)
        finally:
            in_flight -= 1

    await _execute(
        SyncLabelStudioDiveSlateLabelsWorkflow,
        None,
        [stub_projects, stub_sync],
        max_concurrent=50,
    )

    assert PROJECT_CONCURRENCY == 4
    assert peak <= PROJECT_CONCURRENCY, f"peak concurrency was {peak}"
    assert len(started) == 20


async def test_one_failing_project_does_not_stop_the_others():
    """v2 fix: every other project still syncs, and the run fails naming the
    one that did not."""
    synced: List[int] = []

    @activity.defn(name="slate_label_projects")
    async def stub_projects() -> List[LabelProject]:
        return [LabelProject(TENANT, p) for p in (66, 67, 68)]

    @activity.defn(name="sync_slate_labels")
    async def stub_sync(project: LabelProject) -> None:
        if project.ls_project_id == 67:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError("PDF unavailable", non_retryable=True)
        synced.append(project.ls_project_id)

    with pytest.raises(WorkflowFailureError) as excinfo:
        await _execute(
            SyncLabelStudioDiveSlateLabelsWorkflow, None, [stub_projects, stub_sync]
        )

    assert sorted(synced) == [66, 68]
    assert "[67]" in str(excinfo.value.cause)
