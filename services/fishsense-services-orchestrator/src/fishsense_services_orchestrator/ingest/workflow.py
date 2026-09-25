"""Ingest one dive folder from the NAS into a dive and its captures.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/ingest_dive_workflow.py. Behaviour is
v1's. v2 changes: the request names its tenant; the tenant and device that
preflight resolved ride along to the scan and finalize; activities carry their
v2 names; payloads go through the pydantic data converter.

On-demand, no schedule. **One request means one dive** -- the frames are the
`.ORF` files directly inside the named folder, not a recursive walk. That is
precedent, not simplification: the retired spider crawler assigned
`dive = image.parent`, so every dive row in prod is exactly one directory, and
recursing would merge dives that are distinct rows today.

Set `dry_run` to stop after preflight, having written nothing.

**The shape is a two-phase commit, because there is no transaction spanning
these activities.** The dive is created at low -- which every hourly cohort
ignores -- frames are registered in batches, and only then is it promoted to
the requested priority. Priority is the commit flag. A crash anywhere in the
middle leaves a dive and some captures that no pipeline stage will touch, and
re-running is safe: create upserts on the path, the scan skips frames already
registered, and finalize refuses to promote an incomplete set.

Batching exists so a failure costs one batch of downloads rather than the whole
dive; the batch size is deliberately modest because each frame is ~14.5 MB and
the NAS is shared with the hourly staging activities doing real pipeline work.
"""

import uuid
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    # Loaded here, outside the sandbox: pydantic validates lazily, and would
    # otherwise re-import its helpers inside every workflow run.
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ingest.activities import (
        INCOMPLETE_INGEST_TYPE,
        BatchResult,
        DiveFolderListing,
        IngestTotals,
    )
    from fishsense_services_orchestrator.ingest.contracts import (
        IngestDiveRequest,
        IngestPreflight,
        IngestProgress,
        IngestReport,
    )
    from fishsense_services_orchestrator.ingest.nas_errors import (
        NAS_FILE_NOT_FOUND_TYPE,
    )

__all__ = ["BATCH_SIZE", "IngestDiveWorkflow"]

# Frames per scan activity. Each is a whole-file download (~14.5 MB), so 25 is
# roughly 360 MB of work -- small enough that a failure is cheap to redo, large
# enough not to pay activity overhead per frame.
BATCH_SIZE = 25

# A dive's frames are read serially inside the activity, so a batch can take a
# while; the heartbeat is what distinguishes slow from wedged.
_SCAN_TIMEOUT = timedelta(hours=2)

_NAS_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    # A missing file cannot be fixed by waiting.
    non_retryable_error_types=[NAS_FILE_NOT_FOUND_TYPE],
)


@workflow.defn
class IngestDiveWorkflow:
    """List, preflight, create, scan, finalize -- one dive."""

    def __init__(self) -> None:
        self._progress = IngestProgress()

    @workflow.query
    def progress(self) -> IngestProgress:
        """Live counts. A large dive is hours of downloading, so the portal (and
        an operator watching by hand) needs to see movement without waiting for
        the return value."""
        return self._progress

    @workflow.run
    async def run(self, request: IngestDiveRequest) -> IngestReport:
        self._progress.state = "listing"
        listing: DiveFolderListing = await workflow.execute_activity(
            "list_dive_folder",
            args=(request,),
            result_type=DiveFolderListing,
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=_NAS_RETRY,
        )

        self._progress.state = "preflight"
        self._progress.total = len(listing.files)
        preflight: IngestPreflight = await workflow.execute_activity(
            "preflight",
            args=(request, listing),
            result_type=IngestPreflight,
            # Ranged 1 MB reads, serially, over every frame.
            schedule_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=10),
            retry_policy=_NAS_RETRY,
        )

        if request.dry_run or preflight.errors:
            # Errors and a dry run leave by the same door: nothing has been
            # written either way, and the report IS the deliverable.
            self._progress.state = "rejected" if preflight.errors else "dry-run"
            return IngestReport(
                dive_path=request.dive_path,
                total=len(listing.files),
                committed=False,
                preflight=preflight,
            )
        # A preflight without errors resolved its tenant (v2).
        tenant_id = preflight.tenant_id

        self._progress.state = "creating"
        dive_id: uuid.UUID = await workflow.execute_activity(
            "create_dive",
            args=(request, preflight),
            result_type=uuid.UUID,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._progress.dive_id = dive_id

        self._progress.state = "scanning"
        totals = IngestTotals(total=len(preflight.images))
        paths: List[str] = [image.path for image in preflight.images]

        for start in range(0, len(paths), BATCH_SIZE):
            batch = paths[start : start + BATCH_SIZE]
            self._progress.current_path = batch[0]
            result: BatchResult = await workflow.execute_activity(
                "scan_and_register",
                args=(tenant_id, dive_id, batch, preflight.resolved_device_id),
                result_type=BatchResult,
                schedule_to_close_timeout=_SCAN_TIMEOUT,
                heartbeat_timeout=timedelta(minutes=15),
                retry_policy=_NAS_RETRY,
            )
            totals.registered += result.registered
            totals.skipped_existing += result.skipped_existing
            totals.rejected.extend(result.rejected)
            if result.max_taken_datetime is not None and (
                totals.max_taken_datetime is None
                or result.max_taken_datetime > totals.max_taken_datetime
            ):
                totals.max_taken_datetime = result.max_taken_datetime

            self._progress.scanned += len(batch)
            self._progress.registered = totals.registered
            self._progress.skipped_existing = totals.skipped_existing
            self._progress.rejected = len(totals.rejected)

        self._progress.state = "finalizing"
        self._progress.current_path = None
        report: IngestReport = await workflow.execute_activity(
            "finalize_dive",
            args=(tenant_id, dive_id, request, totals),
            result_type=IngestReport,
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                # An incomplete set is a data problem: retrying re-reads the
                # same bytes and reaches the same conclusion.
                non_retryable_error_types=[INCOMPLETE_INGEST_TYPE],
            ),
        )

        self._progress.state = "done"
        report.preflight = preflight
        return report
