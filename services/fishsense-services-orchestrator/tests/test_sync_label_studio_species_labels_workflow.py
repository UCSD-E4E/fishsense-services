"""Workflow contract tests for SyncLabelStudioSpeciesLabelsWorkflow and
UpdateDiveImageGroupsWorkflow.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_sync_label_studio_species_labels_workflow.py (3) and
test_update_dive_image_groups_workflow.py (1). Test names, bodies and reasons
are v1's; v2 adaptations, as in the laser sync's port: projects carry their
tenant, and there is no user-sync step (v2 records Label Studio user ids
directly; PLAN.md §9.14 maps them to people).

v2 change, pinned last (the laser sync's): **one project's failure doesn't
cancel the others.** v1's TaskGroup cancelled every other project mid-sync on
the first failure; here each finishes, and the workflow then fails naming the
ones that didn't.
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

from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.species import workflows as sut
from fishsense_services_orchestrator.species.contracts import (
    SpeciesTarget,
    UpdateDiveImageGroupsResult,
)
from fishsense_services_orchestrator.species.workflows import (
    SyncLabelStudioSpeciesLabelsWorkflow,
    UpdateDiveImageGroupsWorkflow,
)

TENANT = uuid.uuid4()
QUEUE = "test-species-sync"


def _stubs(projects, calls: List, *, fail=(), gate=None):
    @activity.defn(name="species_label_projects")
    async def stub_projects() -> List[LabelProject]:
        calls.append("projects")
        return projects

    @activity.defn(name="sync_species_labels")
    async def stub_sync(project: LabelProject) -> None:
        if gate is not None:
            await gate(project)
        if project.ls_project_id in fail:
            raise ApplicationError("simulated failure", non_retryable=True)
        calls.append(f"sync:{project.ls_project_id}")

    return [stub_projects, stub_sync]


async def _run(workflow_cls, activities, *args, max_concurrent_activities=None):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[workflow_cls],
            activities=activities,
            max_concurrent_activities=max_concurrent_activities or 100,
        ):
            return await env.client.execute_workflow(
                workflow_cls.run,
                *args,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
                execution_timeout=timedelta(seconds=30),
            )


async def test_workflow_invokes_project_ids_then_one_sync_per_project():
    calls: List[str] = []
    projects = [LabelProject(TENANT, p) for p in (70, 57, 58)]

    await _run(SyncLabelStudioSpeciesLabelsWorkflow, _stubs(projects, calls))

    assert calls[0] == "projects"
    assert sorted(calls[1:]) == ["sync:57", "sync:58", "sync:70"]


async def test_workflow_with_no_projects_does_not_invoke_per_project_sync():
    calls: List[str] = []

    await _run(SyncLabelStudioSpeciesLabelsWorkflow, _stubs([], calls))

    assert calls == ["projects"]


async def test_per_project_activity_caps_concurrency_at_workflow_level():
    """Workflow-level cap mirrors laser: PROJECT_CONCURRENCY=4."""
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def gate(_project):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.wait_for(release.wait(), timeout=5.0)
        finally:
            in_flight -= 1

    async def release_soon():
        await asyncio.sleep(0.5)
        release.set()

    calls: List[str] = []
    projects = [LabelProject(TENANT, p) for p in range(20)]
    releaser = asyncio.create_task(release_soon())
    await _run(
        SyncLabelStudioSpeciesLabelsWorkflow,
        _stubs(projects, calls, gate=gate),
        max_concurrent_activities=50,
    )
    await releaser

    assert sut.PROJECT_CONCURRENCY == 4
    assert peak <= 4, f"peak concurrency was {peak}, expected <= 4"
    assert len(calls) == 21


async def test_one_projects_failure_does_not_cancel_the_others():
    calls: List[str] = []
    projects = [LabelProject(TENANT, p) for p in (70, 57, 58)]

    with pytest.raises(WorkflowFailureError) as failure:
        await _run(
            SyncLabelStudioSpeciesLabelsWorkflow, _stubs(projects, calls, fail={57})
        )

    assert sorted(calls[1:]) == ["sync:58", "sync:70"]
    assert "57" in str(failure.value.cause)


async def test_update_groups_forwards_the_dive_to_the_activity_and_returns_its_result():
    seen: List[SpeciesTarget] = []
    target = SpeciesTarget(TENANT, uuid.uuid4())

    @activity.defn(name="update_dive_image_groups")
    async def stub(t: SpeciesTarget) -> UpdateDiveImageGroupsResult:
        seen.append(t)
        return UpdateDiveImageGroupsResult(
            skipped_already_grouped=False,
            new_clusters_created=3,
            species_labels_seen=12,
        )

    result = await _run(UpdateDiveImageGroupsWorkflow, [stub], target)

    assert seen == [target]
    assert result.new_clusters_created == 3
    assert result.species_labels_seen == 12
    assert result.skipped_already_grouped is False
