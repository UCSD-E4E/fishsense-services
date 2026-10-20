"""The render workflows: a mission's captures rendered, and the backlog.

New in v2 (2026-10-19). Per mission (`RenderMissionWorkflow`): stage the
mission's raws once, then a page of owed captures at a time (`RENDER_PAGE`:
their presigned PUTs would not fit one payload, review of #144): the
processor's `RenderCapturesWorkflow` renders each (named as a raw-scratch
reader, so another stage's cleanup waits), and the renders the stored objects
confirm are recorded; clean the scratch up. A tenant with no bucket is
skipped, and the report says so. The automatic frames step renders its
mission's owed captures the same way (`render_pages`), on its own staging.

The backlog (`RenderBacklogWorkflow`) takes every mission owing renders, those
with a human measurement first, `concurrency` at a time -- the next starting
as each finishes -- and a mission that fails is reported, not fatal to the
rest (review of #144); what one run leaves owed (a capture that kept failing,
an unverified render, a raw missing from scratch) the next renders. Hourly
when enabled (`RenderSettings.renders_enabled`), on demand otherwise::

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
from temporalio.exceptions import ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_contracts.renders import RenderResult
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
        RENDER_PAGE,
        RenderPage,
        RenderPersistReport,
        RenderTarget,
    )

__all__ = [
    "RenderBacklogInput",
    "RenderBacklogReport",
    "RenderBacklogWorkflow",
    "RenderMissionFailure",
    "RenderMissionReport",
    "RenderMissionWorkflow",
    "render_child_timeout",
    "render_pages",
]

_DB_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=3,
    non_retryable_error_types=["NotAMember", "InvalidRenders"],
)
_DB_TIMEOUT = timedelta(minutes=15)


def render_child_timeout(n: int) -> timedelta:
    """A page's render child: 30 min, plus 10 s a capture (a render is a few
    seconds on the per-image pod), at most 12 h."""
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
    #: Raws copied into the tenant's bucket, and copies the stored object or
    #: the capture's checksum disagreed with (2026-10-20).
    raws: int = 0
    raws_unverified: int = 0
    no_bucket: bool = False
    #: The mission's render child is running under another firing.
    running_elsewhere: bool = False


@dataclass
class RenderBacklogInput:
    concurrency: int = 2
    #: At most this many missions this run (None: every one owing renders).
    limit: Optional[int] = None


@dataclass
class RenderMissionFailure:
    tenant_id: str
    mission_id: str
    error: str


@dataclass
class RenderBacklogReport:
    missions: List[RenderMissionReport] = field(default_factory=list)
    failed: List[RenderMissionFailure] = field(default_factory=list)


async def _db(name: str, *args, result_type=None):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=_DB_TIMEOUT,
        retry_policy=_DB_RETRY,
        result_type=result_type,
    )


async def _page(target: RenderTarget, after: Optional[int]) -> RenderPage:
    return await _db(
        "resolve_render_inputs", target, after, RENDER_PAGE, result_type=RenderPage
    )


async def render_pages(
    target: RenderTarget, first: Optional[RenderPage] = None
) -> RenderMissionReport:
    """Render the mission's owed captures a page at a time, on raws the caller
    staged, recording what the stored objects confirm. `first` is the first
    page, if the caller has it."""
    report = RenderMissionReport(str(target.tenant_id), str(target.mission_id))
    page = first if first is not None else await _page(target, None)
    woke = False
    while True:
        if page.no_bucket:
            report.no_bucket = True
            return report
        if page.payload is None or not page.payload.frames:
            return report
        if not woke:
            await wake_per_image_processor()
            woke = True
        frames = page.payload.frames
        report.owed += len(frames)
        try:
            results = await workflow.execute_child_workflow(
                "RenderCapturesWorkflow",
                page.payload,
                id=raw_scratch_reader_id("render-captures", target.mission_id),
                task_queue=PROCESSOR_TASK_QUEUE,
                execution_timeout=render_child_timeout(len(frames)),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[RenderResult],
            )
        except WorkflowAlreadyStartedError:
            report.running_elsewhere = True
            return report
        persisted: RenderPersistReport = await _db(
            "persist_renders",
            target,
            page.payload,
            results,
            result_type=RenderPersistReport,
        )
        report.rendered += persisted.rendered
        report.abstained += persisted.abstained
        report.unverified += len(persisted.unverified)
        report.raws += persisted.raws
        report.raws_unverified += len(persisted.raws_unverified)
        report.left_out += len(frames) - len(results)
        page = await _page(target, page.last_number)


@workflow.defn(name="RenderMissionWorkflow")
class RenderMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: RenderTarget) -> RenderMissionReport:
        first = await _page(target, None)
        if first.no_bucket or first.payload is None or not first.payload.frames:
            report = RenderMissionReport(str(target.tenant_id), str(target.mission_id))
            report.no_bucket = first.no_bucket
            return report
        staging = StagingTarget(target.tenant_id, target.mission_id)
        report: Optional[RenderMissionReport] = None
        try:
            await stage_raw(staging)
            report = await render_pages(target, first)
            return report
        finally:
            # The scratch is this run's to clean up -- unless its render child
            # was running under another firing, which owns it.
            if report is None or not report.running_elsewhere:
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
        # A sliding window: the next mission starts as each finishes (review of
        # #144: fixed batches waited on their slowest).
        slots = asyncio.Semaphore(max(1, args.concurrency))

        async def one(target: RenderTarget) -> RenderMissionReport:
            async with slots:
                return await workflow.execute_child_workflow(
                    RenderMissionWorkflow.run,
                    target,
                    id=f"render-mission-{target.mission_id}",
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=RenderMissionReport,
                )

        outcomes = await asyncio.gather(
            *[one(t) for t in targets], return_exceptions=True
        )
        report = RenderBacklogReport()
        for target, outcome in zip(targets, outcomes):
            if isinstance(outcome, (ChildWorkflowError, WorkflowAlreadyStartedError)):
                cause = getattr(outcome, "cause", None) or outcome
                workflow.logger.warning(
                    "renders for mission=%s failed: %s", target.mission_id, cause
                )
                report.failed.append(
                    RenderMissionFailure(
                        str(target.tenant_id), str(target.mission_id), str(cause)
                    )
                )
            elif isinstance(outcome, BaseException):
                raise outcome  # cancellation, or a bug: not a mission's failure
            else:
                report.missions.append(outcome)
        return report
