"""The wake steps the parents call (nrp.workflow), as a parent calls them.

Ported from fishsense-lite@77e8f8e5 _dispatch.py's `wake_data_worker`,
`wake_light_worker` and `wake_gpu_worker`. Stage 1 exercises the light wake;
the per-image and GPU wakes wait for their slices' parents, so this pins them
now: each runs the activity its role's stage builds, and the GPU wake hands
its mode back, since a parent must not dispatch on ``unavailable``.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_orchestrator.nrp.workflow import (
    wake_gpu_processor,
    wake_light_processor,
    wake_per_image_processor,
)


@workflow.defn(name="WakeEveryRole")
class _WakeEveryRole:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self) -> str:
        await wake_per_image_processor()
        await wake_light_processor()
        return await wake_gpu_processor()


@pytest.mark.parametrize("mode", ["gpu", "cpu_fallback", "unavailable"])
async def test_each_wake_runs_its_roles_activity_and_the_gpu_mode_comes_back(mode):
    woken: list[str] = []

    @activity.defn(name="ensure_per_image_processor_running")
    async def per_image() -> int:
        woken.append("per_image")
        return 1

    @activity.defn(name="ensure_light_processor_running")
    async def light() -> int:
        woken.append("light")
        return 1

    @activity.defn(name="ensure_gpu_processor_running")
    async def gpu() -> str:
        woken.append("gpu")
        return mode

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-wakes",
            workflows=[_WakeEveryRole],
            activities=[per_image, light, gpu],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            result = await env.client.execute_workflow(
                "WakeEveryRole",
                id=f"test-wakes-{uuid.uuid4()}",
                task_queue="test-wakes",
                result_type=str,
            )

    assert woken == ["per_image", "light", "gpu"]
    assert result == mode
