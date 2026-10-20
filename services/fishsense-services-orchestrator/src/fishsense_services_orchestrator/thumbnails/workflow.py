"""The thumbnail backlog: every tenant's renders owing a thumbnail.

New in v2 (2026-10-20). Per tenant with a bucket, a page at a time
(`THUMBNAIL_PAGE`): the processor's `MakeThumbnailsWorkflow` reads each render
through its presigned GET, scales it and PUTs the thumbnail; what the stored
objects confirm is recorded. The per-image processor is woken only when a page
is owed. A page that records nothing -- every thumbnail failing its tries,
left owed -- stops its tenant for this run, or it would be handed out again
forever. Hourly when enabled (`RenderSettings.thumbnails_enabled`), on demand
otherwise::

    temporal workflow start --task-queue fishsense_orchestrator \\
        --type ThumbnailBacklogWorkflow --workflow-id thumbnail-backlog-<date>
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

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

__all__ = ["ThumbnailBacklogReport", "ThumbnailBacklogWorkflow"]

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
    async def run(self, _args: Optional[dict] = None) -> ThumbnailBacklogReport:
        report = ThumbnailBacklogReport()
        tenants: List[uuid.UUID] = await _db(
            "select_thumbnail_tenants", result_type=List[uuid.UUID]
        )
        woke = False
        for tenant_id in tenants:
            while True:
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
                results = await workflow.execute_child_workflow(
                    "MakeThumbnailsWorkflow",
                    page.payload,
                    id=f"{workflow.info().workflow_id}-{tenant_id}-{report.pages}",
                    task_queue=PROCESSOR_TASK_QUEUE,
                    execution_timeout=timedelta(hours=2),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=List[ThumbnailResult],
                )
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
                if persisted.made + persisted.missing == 0:
                    workflow.logger.warning(
                        "thumbnails: tenant=%s page recorded nothing; next run",
                        tenant_id,
                    )
                    break
        return report
