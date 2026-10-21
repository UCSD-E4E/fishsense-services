"""The thumbnail backlog: every tenant's renders owing a thumbnail.

New in v2 (2026-10-20). Per tenant with a bucket, a page at a time
(`THUMBNAIL_PAGE`): the processor's `MakeThumbnailsWorkflow` reads each render
through its presigned GET, scales it and PUTs the thumbnail; what the stored
objects confirm is recorded. The per-image processor is woken only when a page
is owed. A page that records nothing -- every thumbnail failing its tries,
left owed -- stops its tenant for this run, or it would be handed out again
forever. A run hands on to a fresh one (continue-as-new) after
`PAGES_PER_RUN` pages, carrying its tenants, tally and wake: #169
(2026-10-20), every page's presigned URLs stayed in one history and Temporal
terminated the backlog about 15,000 thumbnails in. Hourly when enabled
(`RenderSettings.thumbnails_enabled`), on demand otherwise::

    temporal workflow start --task-queue fishsense_orchestrator \\
        --type ThumbnailBacklogWorkflow --workflow-id thumbnail-backlog-<date>
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from dataclasses import field

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_contracts.thumbnails import ThumbnailResult
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_per_image_processor,
    )
    from fishsense_services_orchestrator.thumbnails.activities import (
        THUMBNAIL_PAGE,
        ThumbnailPage,
        ThumbnailPersistReport,
    )

__all__ = [
    "PAGES_PER_RUN",
    "ThumbnailBacklogCarry",
    "ThumbnailBacklogReport",
    "ThumbnailBacklogWorkflow",
]

#: Pages one run makes before it continues as new. A page's input and result
#: carry up to 500 presigned URLs (~600 characters each), so a page adds over
#: a megabyte of history; #169's run was terminated near page 30.
PAGES_PER_RUN = 10

_DB_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=3,
    non_retryable_error_types=["NotAMember", "InvalidThumbnails"],
)
_DB_TIMEOUT = timedelta(minutes=15)


@dataclass
class ThumbnailBacklogReport:
    pages: int = 0
    made: int = 0
    missing: int = 0
    unverified: int = 0
    #: Tenants whose page's child failed; the run went on to the next.
    failed_tenants: List[str] = field(default_factory=list)


@dataclass
class ThumbnailBacklogCarry:
    """What a run hands the next: the tenants still to page through (the
    first one part-way), the tally so far, and whether it woke the
    processor."""

    tenants: Optional[List[uuid.UUID]] = None
    report: ThumbnailBacklogReport = field(default_factory=ThumbnailBacklogReport)
    woke: bool = False


async def _db(name: str, *args, result_type=None):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=_DB_TIMEOUT,
        retry_policy=_DB_RETRY,
        result_type=result_type,
    )


@workflow.defn(name="ThumbnailBacklogWorkflow")
class ThumbnailBacklogWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, carry: Optional[ThumbnailBacklogCarry] = None
    ) -> ThumbnailBacklogReport:
        carry = carry or ThumbnailBacklogCarry()
        report = carry.report
        tenants: List[uuid.UUID] = (
            carry.tenants
            if carry.tenants is not None
            else await _db("select_thumbnail_tenants", result_type=List[uuid.UUID])
        )
        woke = carry.woke
        pages_this_run = 0
        for at, tenant_id in enumerate(tenants):
            while True:
                if pages_this_run >= PAGES_PER_RUN and workflow.patched(
                    "thumbnail-backlog-continue-as-new"
                ):
                    workflow.continue_as_new(
                        ThumbnailBacklogCarry(
                            tenants=tenants[at:], report=report, woke=woke
                        )
                    )
                page: ThumbnailPage = await _db(
                    "resolve_thumbnails",
                    tenant_id,
                    THUMBNAIL_PAGE,
                    result_type=ThumbnailPage,
                )
                if page.payload is None or not page.payload.frames:
                    break
                if not woke:
                    await wake_per_image_processor()
                    woke = True
                report.pages += 1
                pages_this_run += 1
                try:
                    results = await workflow.execute_child_workflow(
                        "MakeThumbnailsWorkflow",
                        page.payload,
                        id=f"{workflow.info().workflow_id}-{tenant_id}-{report.pages}",
                        task_queue=PROCESSOR_TASK_QUEUE,
                        execution_timeout=timedelta(hours=2),
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                        result_type=List[ThumbnailResult],
                    )
                except ChildWorkflowError as error:
                    # One tenant's failure is not the run's (review of the
                    # stack): the next tenant still gets its thumbnails.
                    workflow.logger.warning(
                        "thumbnails: tenant=%s failed: %s",
                        tenant_id,
                        error.cause or error,
                    )
                    report.failed_tenants.append(str(tenant_id))
                    break
                persisted: ThumbnailPersistReport = await _db(
                    "persist_thumbnails",
                    tenant_id,
                    page.payload,
                    results,
                    result_type=ThumbnailPersistReport,
                )
                report.made += persisted.made
                report.missing += persisted.missing
                report.unverified += len(persisted.unverified)
                if persisted.made + persisted.missing == 0 and not persisted.failed:
                    workflow.logger.warning(
                        "thumbnails: tenant=%s page recorded nothing; next run",
                        tenant_id,
                    )
                    break
        return report
