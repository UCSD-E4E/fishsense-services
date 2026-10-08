"""The path-repair workflow: on demand, one dive, a dry run unless told.

    temporal workflow start --task-queue fishsense_orchestrator \\
        --type RepairMovedCapturePathsWorkflow --input 249            # dry run
    ... --input 249 --input true                                      # apply
"""

from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.ops.paths.contracts import PathRepairReport
from fishsense_services_orchestrator.ops.paths.workflow import (
    RepairMovedCapturePathsWorkflow,
)

QUEUE = "test-repair-paths"


async def _execute(args):
    calls: list[tuple] = []

    @activity.defn(name="repair_moved_capture_paths")
    async def repair(dive_number: int, apply: bool) -> PathRepairReport:
        calls.append((dive_number, apply))
        return PathRepairReport(dive_number=dive_number, applied=apply, frames=3)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[RepairMovedCapturePathsWorkflow],
            activities=[repair],
        ):
            report = await env.client.execute_workflow(
                RepairMovedCapturePathsWorkflow.run,
                args=args,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
    return report, calls


async def test_the_dive_alone_is_a_dry_run():
    """`--input 249` is the obvious way to start it, and must change nothing."""
    report, calls = await _execute((249,))
    assert calls == [(249, False)]
    assert report.applied is False
    assert isinstance(report, PathRepairReport)


async def test_apply_is_explicit():
    report, calls = await _execute((249, True))
    assert calls == [(249, True)]
    assert report.applied is True
