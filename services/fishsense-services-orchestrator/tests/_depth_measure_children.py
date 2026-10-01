"""Stand-ins for the processor's laser-depth and measure-fish workflows.

In their own module, with no module-level state: the workflow sandbox
re-imports a workflow's module to validate it, and `uuid.uuid4()` is
restricted there, so the tests' fixtures must not be built at import time
alongside these.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.laser_depth import (
        ComputeLaserDepthsInput,
        ComputeLaserDepthsResult,
    )
    from fishsense_services_contracts.measurement import (
        MeasureFishInput,
        MeasureFishResult,
    )


def depth_result(payload: ComputeLaserDepthsInput) -> ComputeLaserDepthsResult:
    return ComputeLaserDepthsResult(
        dive_id=payload.dive_id, core_version="4.1.0", captures=[]
    )


def measure_result(payload: MeasureFishInput) -> MeasureFishResult:
    return MeasureFishResult(
        dive_id=payload.dive_id,
        algorithm="laser_depth_fronto_parallel",
        algorithm_version="1",
        core_version="4.1.0",
        captures=[],
    )


async def _record(payload) -> None:
    await workflow.execute_activity(
        "_record_child_dispatch",
        args=(workflow.info().workflow_id, payload.dive_id, len(payload.captures)),
        schedule_to_close_timeout=timedelta(seconds=5),
    )


@workflow.defn(name="ComputeLaserDepthsWorkflow")
class StubDepthChild:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: ComputeLaserDepthsInput) -> ComputeLaserDepthsResult:
        await _record(payload)
        return depth_result(payload)


@workflow.defn(name="MeasureFishWorkflow")
class StubMeasureChild:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: MeasureFishInput) -> MeasureFishResult:
        await _record(payload)
        return measure_result(payload)


@workflow.defn(name="ComputeLaserDepthsWorkflow")
class HangingDepthChild:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: ComputeLaserDepthsInput) -> ComputeLaserDepthsResult:
        await workflow.wait_condition(lambda: False)
        return depth_result(payload)
