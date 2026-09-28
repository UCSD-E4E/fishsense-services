"""Workflow contract tests for the processor's laser-depth and measure workflows.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_compute_laser_depths_workflow.py and
test_measure_fish_workflow.py: the workflow hands its payload to its one
activity and returns what it returns. v2 adaptation: the payload is the
processing contract's input rather than a bare dive id, and both stages serve
the light role (v1's light queue: no image bytes, rows in, numpy, rows out).
"""

from __future__ import annotations

import uuid
from typing import List

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    ComputeLaserDepthsResult,
    LaserCalibrationGeometry,
)
from fishsense_services_contracts.measurement import MeasureFishInput, MeasureFishResult
from fishsense_services_processor.laser_depth.stage import STAGE as LASER_DEPTH
from fishsense_services_processor.laser_depth.workflow import ComputeLaserDepthsWorkflow
from fishsense_services_processor.measurement.stage import STAGE as MEASUREMENT
from fishsense_services_processor.measurement.workflow import MeasureFishWorkflow
from fishsense_services_processor.registry import ROLE_LIGHT

K = ((3000.0, 0.0, 2048.0), (0.0, 3000.0, 1536.0), (0.0, 0.0, 1.0))
DIVE = uuid.uuid4()
GEOMETRY = LaserCalibrationGeometry(
    laser_calibration_id=uuid.uuid4(),
    laser_position=(-0.03, -0.10, 0.0),
    laser_axis=(0.0, -0.02, 1.0),
)


async def _execute(workflow_cls, activity_fn, payload, result_type):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-light",
            workflows=[workflow_cls],
            activities=[activity_fn],
        ):
            return await env.client.execute_workflow(
                workflow_cls.run,
                payload,
                id=f"test-{uuid.uuid4()}",
                task_queue="test-light",
                result_type=result_type,
            )


async def test_depth_workflow_invokes_activity_with_payload_and_returns_result():
    calls: List[ComputeLaserDepthsInput] = []
    expected = ComputeLaserDepthsResult(dive_id=DIVE, core_version="4.1.0", captures=[])

    @activity.defn(name="compute_laser_depths")
    async def stub(payload: ComputeLaserDepthsInput) -> ComputeLaserDepthsResult:
        calls.append(payload)
        return expected

    payload = ComputeLaserDepthsInput(
        dive_id=DIVE, camera_matrix=K, calibration=GEOMETRY, captures=[]
    )
    result = await _execute(
        ComputeLaserDepthsWorkflow, stub, payload, ComputeLaserDepthsResult
    )

    assert calls == [payload]
    assert result == expected


async def test_measure_workflow_invokes_activity_with_payload_and_returns_result():
    calls: List[MeasureFishInput] = []
    expected = MeasureFishResult(
        dive_id=DIVE,
        algorithm="laser_depth_fronto_parallel",
        algorithm_version="1",
        core_version="4.1.0",
        captures=[],
    )

    @activity.defn(name="measure_fish")
    async def stub(payload: MeasureFishInput) -> MeasureFishResult:
        calls.append(payload)
        return expected

    payload = MeasureFishInput(
        dive_id=DIVE, camera_matrix=K, calibration=GEOMETRY, captures=[]
    )
    result = await _execute(MeasureFishWorkflow, stub, payload, MeasureFishResult)

    assert calls == [payload]
    assert result == expected


def test_both_stages_serve_the_light_role():
    """v1's light queue (fishsense-lite@77e8f8e5 roles.py): these hold no
    image bytes, so they must not wait behind the per-image role's memory
    cap."""
    assert LASER_DEPTH.role == ROLE_LIGHT
    assert MEASUREMENT.role == ROLE_LIGHT
    assert LASER_DEPTH.workflows == [ComputeLaserDepthsWorkflow]
    assert MEASUREMENT.workflows == [MeasureFishWorkflow]
