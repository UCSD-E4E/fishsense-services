"""Workflow contract test for PerformLaserCalibrationWorkflow.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_perform_laser_calibration_workflow.py. Drives the
workflow end-to-end against an in-process Temporal test server. The activity
is replaced with a stub that records its payload so we can assert the
workflow forwards the input and propagates the activity's return value
unchanged.

v2 changes: the input is a `SlateCalibrationInput` (v1: a bare dive id; its
data-worker read the rest through the SDK), and the result is a
`LaserCalibrationResult` for the orchestrator to persist (v1: the persisted
row id, or None). v1's "None when no slate" case moved to the orchestrator,
which no longer dispatches such a dive; its twin here is a refusal, which must
also come back unchanged rather than failing the workflow.
"""

from __future__ import annotations

import uuid
from typing import List

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.slate_calibration import (
    LaserCalibrationResult,
    SlateCalibrationInput,
)
from fishsense_services_processor.laser_calibration.workflow import (
    PerformLaserCalibrationWorkflow,
)

DIVE = uuid.UUID(int=427)
PAYLOAD = SlateCalibrationInput(
    dive_id=DIVE,
    camera_matrix=[[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]],
    template_points=[(0.0, 0.0)],
    dpi=300,
    observations=[],
    dive_dots=[],
)
ACCEPTED = LaserCalibrationResult(
    outcome="accepted",
    laser_position=[0.06, 0.08, 0.0],
    laser_axis=[0.0, 0.0, 1.0],
    observation_count=6,
    observations_trimmed=0,
    gate_verdicts={},
    core_version="4.1.0",
)
REFUSED = LaserCalibrationResult(
    outcome="refused",
    refusal_type="InsufficientLaserPoints",
    refusal_reason="insufficient laser points (0 < 2)",
    observation_count=0,
    observations_trimmed=0,
    gate_verdicts={},
    core_version="4.1.0",
)


async def _run(returns: LaserCalibrationResult, calls: List):
    @activity.defn(name="perform_laser_calibration")
    async def stub_activity(payload: SlateCalibrationInput) -> LaserCalibrationResult:
        calls.append(payload)
        return returns

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-stage13",
            workflows=[PerformLaserCalibrationWorkflow],
            activities=[stub_activity],
        ):
            return await env.client.execute_workflow(
                PerformLaserCalibrationWorkflow.run,
                PAYLOAD,
                id=f"test-stage13-{uuid.uuid4()}",
                task_queue="test-stage13",
                result_type=LaserCalibrationResult,
            )


async def test_workflow_invokes_activity_with_its_input_and_returns_its_result():
    calls: List = []

    result = await _run(ACCEPTED, calls)

    assert calls == [PAYLOAD]
    assert result == ACCEPTED


async def test_workflow_propagates_a_refusal_unchanged():
    """The orchestrator records a refusal and fails loud; the child only
    carries it back, so a refusal is data here, not an error."""
    result = await _run(REFUSED, [])

    assert result == REFUSED
