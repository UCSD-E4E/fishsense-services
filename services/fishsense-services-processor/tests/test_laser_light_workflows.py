"""The light role's laser workflows: the gate, the validator, the plan.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/ (test_evaluate_laser_auto_accept_workflow_timeouts.py,
test_validate_laser_labels_for_dive_workflow.py, and the plan half of
test_remediate_laser_supersedes_workflow.py). v1's reasons, kept: the gate
declares the shared timeout budget rather than its own literals, and queue
wait is bounded separately from execution. v2 changes: each workflow is handed
rows and returns what to write (the processor never touches the database), so
they run the real activities end to end here; the remediation workflow plans
one dive, and the orchestrator's parent runs the whole report.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.laser import (
    GATE_ACTIVITY_TIMEOUT,
    GATE_EXECUTION_TIMEOUT,
    GATE_QUEUE_WAIT_TIMEOUT,
    EvaluateLaserAutoAcceptInput,
    LaserAutoAcceptResult,
    LaserAutoAcceptSummary,
    PlanLaserRemediationInput,
    ValidateLaserLabelsInput,
)
from fishsense_services_processor import registry
from fishsense_services_processor.laser_validation import workflows as sut
from fishsense_services_processor.laser_validation.stage import STAGE

from ._laser import DIVE, colinear_labels, prod_like_dive

QUEUE = "test-laser-light"


@pytest.fixture(name="dispatch")
def _dispatch(monkeypatch):
    """Run the gate workflow's body with a recording stub in place of Temporal."""
    calls: dict = {}

    async def _record(activity_name, *args, **kwargs):
        calls["activity_name"] = activity_name
        calls["args"] = args
        calls.update(kwargs)
        return LaserAutoAcceptResult(
            summary=LaserAutoAcceptSummary(dive_id=DIVE), frames=[]
        )

    monkeypatch.setattr(workflow, "execute_activity", _record)
    payload = EvaluateLaserAutoAcceptInput(dive_id=DIVE, dive_number=7, predictions=[])
    asyncio.run(sut.EvaluateLaserAutoAcceptWorkflow().run(payload))
    return calls


def test_it_dispatches_the_gate_activity(dispatch):
    assert dispatch["activity_name"] == "evaluate_laser_auto_accept"


def test_queue_wait_is_bounded_separately_from_execution(dispatch):
    assert dispatch["schedule_to_start_timeout"] == GATE_QUEUE_WAIT_TIMEOUT
    assert dispatch["start_to_close_timeout"] == GATE_EXECUTION_TIMEOUT


def test_the_close_bound_leaves_room_for_a_late_start(dispatch):
    assert dispatch["schedule_to_close_timeout"] == GATE_ACTIVITY_TIMEOUT


def test_the_gate_still_heartbeats(dispatch):
    assert dispatch["heartbeat_timeout"] == timedelta(minutes=1)


async def _run(workflow_run, payload):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=list(STAGE.workflows),
            activities=list(STAGE.activities),
        ):
            return await env.client.execute_workflow(
                workflow_run,
                payload,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


async def test_the_validation_workflow_returns_what_to_write():
    labels = colinear_labels(40)
    labels[5].y += 50.0

    result = await _run(
        sut.ValidateLaserLabelsForDiveWorkflow.run,
        ValidateLaserLabelsInput(dive_id=DIVE, labels=labels),
    )

    assert [s.label_id for s in result.supersede] == [labels[5].label_id]
    assert result.line is not None


async def test_the_plan_workflow_returns_one_dives_plan():
    labels = prod_like_dive(superseded={9})

    plan = await _run(
        sut.PlanLaserSupersedeRemediationWorkflow.run,
        PlanLaserRemediationInput(dive_id=7, labels=labels),
    )

    assert plan.revive_ids == [labels[9].number]


def test_the_laser_math_is_a_light_stage():
    """No image bytes: rows in, numpy, rows out. It must not wait behind the
    per-image role's memory cap (v1 moved the gate and the validator to the
    light queue on 2026-09-04)."""
    registration = registry.registration_for_role(registry.ROLE_LIGHT)
    for wf in (
        sut.EvaluateLaserAutoAcceptWorkflow,
        sut.ValidateLaserLabelsForDiveWorkflow,
        sut.PlanLaserSupersedeRemediationWorkflow,
    ):
        assert wf in registration.workflows
