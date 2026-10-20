"""The render workflows: a mission's captures rendered, and the backlog.

New in v2 (2026-10-19). Per mission (`RenderMissionWorkflow`): resolve the
owed captures and their presigned PUTs; wake the per-image processor; stage
the mission's raws; the processor's `RenderCapturesWorkflow` renders each
(named as a raw-scratch reader, so another stage's cleanup waits); record the
renders the stored objects confirm; clean the scratch up. A tenant with no
bucket is skipped, and the report says so.

The backlog (`RenderBacklogWorkflow`) takes every mission owing renders, those
with a human measurement first, `concurrency` at a time; what one run leaves
owed (a capture that kept failing, an unverified render) the next renders.
Hourly when enabled (`RenderSettings.renders_enabled`), on demand otherwise::

    temporal workflow start --task-queue fishsense_orchestrator \\
        --type RenderBacklogWorkflow --workflow-id render-backlog-<date> \\
        --input '{"concurrency": 2}'
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_contracts.renders import (
        RenderCapturesInput,
        RenderResult,
    )
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_per_image_processor,
    )
    from fishsense_services_orchestrator.object_store.contracts import StagingTarget
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )
    from fishsense_services_orchestrator.renders.activities import (
        RenderPersistReport,
        RenderTarget,
    )

__all__ = [
    "RenderBacklogInput",
    "RenderBacklogReport",
    "RenderBacklogWorkflow",
    "RenderMissionReport",
    "RenderMissionWorkflow",
    "render_child_timeout",
]

_DB_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=3,
    non_retryable_error_types=["NotAMember", "InvalidRenders"],
)
_DB_TIMEOUT = timedelta(minutes=15)


def render_child_timeout(n: int) -> timedelta:
    """A mission's render child: 30 min, plus 10 s a capture (a render is a
    few seconds on the per-image pod), at most 12 h."""
    return min(timedelta(minutes=30) + n * timedelta(seconds=10), timedelta(hours=12))


@dataclass
class RenderMissionReport:
    tenant_id: str
    mission_id: str
    owed: int = 0
    rendered: int = 0
    abstained: int = 0
    unverified: int = 0
    #: Owed captures the processor left out after its tries.
    left_out: int = 0
    no_bucket: bool = False
    #: The mission's render child is running under another firing.
    running_elsewhere: bool = False


@dataclass
class RenderBacklogInput:
    concurrency: int = 2
    #: At most this many missions this run (None: every one owing renders).
    limit: Optional[int] = None


@dataclass
class RenderBacklogReport:
    missions: List[RenderMissionReport] = field(default_factory=list)


async def _db(name: str, *args, result_type=None):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=_DB_TIMEOUT,
        retry_policy=_DB_RETRY,
        result_type=result_type,
    )


@workflow.defn(name="RenderMissionWorkflow")
class RenderMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: RenderTarget) -> RenderMissionReport:
        report = RenderMissionReport(str(target.tenant_id), str(target.mission_id))
        payload: Optional[RenderCapturesInput] = await _db(
            "resolve_render_inputs", target, result_type=Optional[RenderCapturesInput]
        )
        if payload is None:
            report.no_bucket = True
            return report
        report.owed = len(payload.frames)
        if not payload.frames:
            return report
        await wake_per_image_processor()
        staging = StagingTarget(target.tenant_id, target.mission_id)
        owns_scratch = True
        try:
            await stage_raw(staging)
            try:
                results = await workflow.execute_child_workflow(
                    "RenderCapturesWorkflow",
                    payload,
                    id=raw_scratch_reader_id("render-captures", target.mission_id),
                    task_queue=PROCESSOR_TASK_QUEUE,
                    execution_timeout=render_child_timeout(len(payload.frames)),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=List[RenderResult],
                )
            except WorkflowAlreadyStartedError:
                owns_scratch = False
                report.running_elsewhere = True
                return report
            persisted: RenderPersistReport = await _db(
                "persist_renders",
                target,
                payload,
                results,
                result_type=RenderPersistReport,
            )
            report.rendered = persisted.rendered
            report.abstained = persisted.abstained
            report.unverified = len(persisted.unverified)
            report.left_out = len(payload.frames) - len(results)
            return report
        finally:
            if owns_scratch:
                await cleanup_raw(staging)


@workflow.defn(name="RenderBacklogWorkflow")
class RenderBacklogWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, args: Optional[RenderBacklogInput] = None
    ) -> RenderBacklogReport:
        args = args or RenderBacklogInput()
        targets: List[RenderTarget] = await _db(
            "select_render_missions", args.limit, result_type=List[RenderTarget]
        )
        workflow.logger.info("renders: %d missions owe renders", len(targets))
        report = RenderBacklogReport()
        step = max(1, args.concurrency)
        for i in range(0, len(targets), step):
            batch = targets[i : i + step]
            report.missions.extend(
                await asyncio.gather(
                    *[
                        workflow.execute_child_workflow(
                            RenderMissionWorkflow.run,
                            t,
                            id=f"render-mission-{t.mission_id}",
                            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                            result_type=RenderMissionReport,
                        )
                        for t in batch
                    ]
                )
            )
        return report
