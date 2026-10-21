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
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from fishsense_services_contracts.temporal import DATA_CONVERTER
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_orchestrator.nrp.workflow import (
    GPU_WAKE_TIMEOUT,
    WAKE_TIMEOUT,
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
        data_converter=DATA_CONVERTER
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


async def test_each_wake_budgets_what_its_constant_says():
    """A parent's run-timeout arithmetic sums these constants (the laser gate
    drain's, for one), so they must be what the wakes actually schedule."""
    budgets: dict[str, object] = {}

    def _record(name, result):
        @activity.defn(name=name)
        async def wake():
            budgets[name] = activity.info().schedule_to_close_timeout
            return result

        return wake

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-wake-budgets",
            workflows=[_WakeEveryRole],
            activities=[
                _record("ensure_per_image_processor_running", 1),
                _record("ensure_light_processor_running", 1),
                _record("ensure_gpu_processor_running", "gpu"),
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await env.client.execute_workflow(
                "WakeEveryRole",
                id=f"test-wake-budgets-{uuid.uuid4()}",
                task_queue="test-wake-budgets",
                result_type=str,
            )

    assert budgets == {
        "ensure_per_image_processor_running": WAKE_TIMEOUT,
        "ensure_light_processor_running": WAKE_TIMEOUT,
        "ensure_gpu_processor_running": GPU_WAKE_TIMEOUT,
    }


# -- a failed per-image wake with workers live (#158) ---------------------------


@workflow.defn(name="WakePerImage")
class _WakePerImage:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self) -> str:
        await wake_per_image_processor()
        return "dispatched"


async def _wake_per_image_with(pollers: int) -> tuple[str, list[str]]:
    calls: list[str] = []

    @activity.defn(name="ensure_per_image_processor_running")
    async def failing_wake() -> int:
        calls.append("wake")
        raise ApplicationError("NRP stand-up timed out", non_retryable=True)

    @activity.defn(name="count_per_image_pollers")
    async def count() -> int:
        calls.append("count")
        return pollers

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-wake-per-image",
            workflows=[_WakePerImage],
            activities=[failing_wake, count],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            result = await env.client.execute_workflow(
                "WakePerImage",
                id=f"test-wake-per-image-{uuid.uuid4()}",
                task_queue="test-wake-per-image",
                result_type=str,
            )
    return result, calls


async def test_a_failed_wake_with_per_image_workers_live_lets_the_parent_proceed():
    """#158, mission 422 (2026-10-20): the NRP stand-up timed out while the
    lab workers polled the queue, and the error failed the mission and threw
    away its staged raws. With workers live, the work gets served: go on."""
    result, calls = await _wake_per_image_with(pollers=2)

    assert result == "dispatched"
    assert calls == ["wake", "count"]


async def test_a_failed_wake_with_no_worker_anywhere_fails_as_before():
    """Nobody serves the queue: a child dispatched now would hang until its
    timeout, so the wake's failure stands."""
    with pytest.raises(WorkflowFailureError):
        await _wake_per_image_with(pollers=0)
