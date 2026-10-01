"""Workflow contract tests for species create/populate and the scheduled
populate parent.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/: test_populate_species_label_studio_project_parent_workflow.py (2),
test_populate_workflow_retry_policy.py (4, the species stage) and
test_populate_label_studio_project_workflows.py (the species cases). Test
names, bodies and reasons are v1's; v2 adaptations: the target is (tenant,
dive), and the activity names are v2's.

v1's rules, kept: create then populate, in that order, with v1's timeouts; the
populate activity's retries are bounded (5 attempts, from 30 s) yet still ride
out an ordinary Label Studio blip; the parent fans out one child per dive,
`populate-species-{dive}`, ALLOW_DUPLICATE, at most four at a time, and one
dive's failure doesn't abort the rest.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Dict, List

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.species import workflows as sut
from fishsense_services_orchestrator.species.contracts import SpeciesTarget
from fishsense_services_orchestrator.species.workflows import (
    CreateSpeciesLabelStudioProjectWorkflow,
    PopulateSpeciesLabelStudioProjectParentWorkflow,
    PopulateSpeciesLabelStudioProjectWorkflow,
)

TENANT = uuid.uuid4()


# -- create_then_populate (test_populate_workflow_retry_policy.py) --------------------


@pytest.fixture(name="calls")
def _calls(monkeypatch):
    seen: List[Dict[str, Any]] = []

    async def fake_execute_activity(name, *args, **kwargs):
        seen.append({"name": name, "args": args, **kwargs})
        return 7 if name.startswith("create_") else 42

    monkeypatch.setattr(workflow, "execute_activity", fake_execute_activity)
    return seen


async def test_emits_create_then_populate_in_order(calls):
    target = SpeciesTarget(TENANT, uuid.uuid4())

    result = await sut.create_then_populate(target)

    assert [c["name"] for c in calls] == [
        "create_species_label_studio_project",
        "populate_species_label_studio_project",
    ]
    assert calls[0]["args"] == (target,)
    assert calls[1]["args"] == (target, 7), "populate takes the created project id"
    assert result == 42


async def test_timeouts_are_unchanged(calls):
    await sut.create_then_populate(SpeciesTarget(TENANT, uuid.uuid4()))

    assert calls[0]["schedule_to_close_timeout"] == timedelta(minutes=5)
    assert calls[1]["schedule_to_close_timeout"] == timedelta(minutes=30)
    assert calls[1]["heartbeat_timeout"] == timedelta(minutes=2)


async def test_populate_retries_are_bounded(calls):
    """Unlimited retries let dive 424 reach attempt 10 over 1107 s and leave 23
    copies of three frames."""
    await sut.create_then_populate(SpeciesTarget(TENANT, uuid.uuid4()))

    policy = calls[1].get("retry_policy")
    assert policy is not None, "unlimited retries are what compounded duplicates"
    assert policy.maximum_attempts == sut.POPULATE_MAX_ATTEMPTS
    assert 1 < policy.maximum_attempts <= 5


async def test_the_retry_window_still_absorbs_a_label_studio_blip(calls):
    """Capping attempts alone would have collapsed the window to seconds."""
    await sut.create_then_populate(SpeciesTarget(TENANT, uuid.uuid4()))

    policy = calls[1]["retry_policy"]
    assert policy.initial_interval >= timedelta(seconds=30)
    assert policy.maximum_interval is not None
    window = timedelta()
    interval = policy.initial_interval
    for _ in range(policy.maximum_attempts - 1):
        window += interval
        interval = min(interval * policy.backoff_coefficient, policy.maximum_interval)
    assert window >= timedelta(minutes=5), "must ride out an ordinary LS blip"


# -- the workflows ---------------------------------------------------------------------


def _ls_stubs(created: List, populated: List, *, rows=3):
    @activity.defn(name="create_species_label_studio_project")
    async def create(target: SpeciesTarget) -> int:
        created.append(target)
        return 70

    @activity.defn(name="populate_species_label_studio_project")
    async def populate(target: SpeciesTarget, project_id: int) -> int:
        populated.append((target, project_id))
        return rows

    return [create, populate]


async def _execute(workflows, activities, run, *args):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-species-populate",
            workflows=workflows,
            activities=activities,
        ):
            return await env.client.execute_workflow(
                run,
                *args,
                id=f"test-species-populate-{uuid.uuid4()}",
                task_queue="test-species-populate",
                execution_timeout=timedelta(minutes=5),
            )


async def test_workflow_creates_per_dive_project_then_populates():
    created, populated = [], []
    target = SpeciesTarget(TENANT, uuid.uuid4())

    result = await _execute(
        [PopulateSpeciesLabelStudioProjectWorkflow],
        _ls_stubs(created, populated),
        PopulateSpeciesLabelStudioProjectWorkflow.run,
        target,
    )

    assert created == [target]
    assert populated == [(target, 70)]
    assert result == 3


async def test_workflow_returns_zero_when_populate_has_no_work():
    created, populated = [], []

    result = await _execute(
        [PopulateSpeciesLabelStudioProjectWorkflow],
        _ls_stubs(created, populated, rows=0),
        PopulateSpeciesLabelStudioProjectWorkflow.run,
        SpeciesTarget(TENANT, uuid.uuid4()),
    )

    assert result == 0


async def test_the_create_workflow_returns_the_project_id():
    created, populated = [], []
    target = SpeciesTarget(TENANT, uuid.uuid4())

    result = await _execute(
        [CreateSpeciesLabelStudioProjectWorkflow],
        _ls_stubs(created, populated),
        CreateSpeciesLabelStudioProjectWorkflow.run,
        target,
    )

    assert result == 70
    assert created == [target] and populated == []


@workflow.defn(name="PopulateSpeciesLabelStudioProjectWorkflow")
class _StubPopulateWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: SpeciesTarget) -> int:
        await workflow.execute_activity(
            "_record_populate",
            args=(workflow.info().workflow_id, target),
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return 0


def _parent_stubs(targets, selector_calls, dispatched, *, fail=()):
    @activity.defn(name="select_dives_needing_species_population")
    async def select() -> List[SpeciesTarget]:
        selector_calls.append(1)
        return targets

    @activity.defn(name="_record_populate")
    async def record(workflow_id: str, target: SpeciesTarget) -> None:
        if target.dive_id in fail:
            raise ApplicationError("simulated", non_retryable=True)
        dispatched.append((workflow_id, target))

    return [select, record]


async def test_fans_out_idempotent_populate_child_per_dive():
    dives = [uuid.uuid4() for _ in range(3)]
    targets = [SpeciesTarget(TENANT, d) for d in dives]
    dispatched, selector_calls = [], []

    result = await _execute(
        [PopulateSpeciesLabelStudioProjectParentWorkflow, _StubPopulateWorkflow],
        _parent_stubs(targets, selector_calls, dispatched),
        PopulateSpeciesLabelStudioProjectParentWorkflow.run,
    )

    assert result == targets
    assert len(selector_calls) == 1
    assert {t for _, t in dispatched} == set(targets)
    assert {wid for wid, _ in dispatched} == {f"populate-species-{d}" for d in dives}


def test_at_most_four_dives_populate_at_once():
    """v1's bound: a large backlog must not hammer the hosted Label Studio
    import endpoint."""
    assert sut.POPULATE_CONCURRENCY == 4


async def test_no_dispatch_when_cohort_empty():
    dispatched, selector_calls = [], []

    result = await _execute(
        [PopulateSpeciesLabelStudioProjectParentWorkflow, _StubPopulateWorkflow],
        _parent_stubs([], selector_calls, dispatched),
        PopulateSpeciesLabelStudioProjectParentWorkflow.run,
    )

    assert result == []
    assert len(selector_calls) == 1
    assert not dispatched


async def test_one_dives_failure_does_not_abort_the_fan_out():
    """v1's `ExceptionGroupErrorLogging(suppress=True)`."""
    bad, good = uuid.uuid4(), uuid.uuid4()
    targets = [SpeciesTarget(TENANT, bad), SpeciesTarget(TENANT, good)]
    dispatched, selector_calls = [], []

    result = await _execute(
        [PopulateSpeciesLabelStudioProjectParentWorkflow, _StubPopulateWorkflow],
        _parent_stubs(targets, selector_calls, dispatched, fail={bad}),
        PopulateSpeciesLabelStudioProjectParentWorkflow.run,
    )

    assert result == targets
    assert [t for _, t in dispatched] == [SpeciesTarget(TENANT, good)]
