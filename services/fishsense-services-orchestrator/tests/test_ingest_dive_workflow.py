"""Contract tests for `IngestDiveWorkflow` -- the end-to-end ingest.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_ingest_dive_workflow.py. Test names, bodies and reasons are v1's; v2
adaptations are marked: the activities' v2 names and arguments (the tenant and
device preflight resolved ride along), UUID ids, and the pydantic converter.

In-process Temporal worker with every activity stubbed. What is pinned here is
the *protocol*, not the activities' behaviour:

  * a dry run and a failed preflight both leave without writing anything;
  * the dive is created before any frame is registered, and promoted only after;
  * frames are batched, and the batches' counts add up into one report.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.ingest.activities import (
    BatchResult,
    DiveFolderListing,
    IngestTotals,
)
from fishsense_services_orchestrator.ingest.contracts import (
    IngestDiveRequest,
    IngestPreflight,
    IngestReport,
    PreflightImage,
)
from fishsense_services_orchestrator.ingest.nas import NasEntry
from fishsense_services_orchestrator.ingest.workflow import (
    BATCH_SIZE,
    IngestDiveWorkflow,
)

TASK_QUEUE = "test-ingest"
FOLDER = "2024.06.20.REEF/082929_FishModels_FSL07"
T = datetime(2024, 8, 21, 9, 30, 0, tzinfo=timezone.utc)
TENANT = uuid.uuid4()
DEVICE = uuid.uuid4()
DIVE = uuid.uuid4()


def _activities(calls, *, frames=2, errors=(), rejected=()):
    images = [
        PreflightImage(path=f"{FOLDER}/{i:04d}.ORF", size=1, taken_datetime=T)
        for i in range(frames)
    ]

    @activity.defn(name="list_dive_folder")
    async def _list(request: IngestDiveRequest) -> DiveFolderListing:
        calls.append("list")
        return DiveFolderListing(
            folder_path=f"/root/{request.dive_path}",
            files=[
                NasEntry(path=i.path, name=i.path[-8:], is_dir=False, size=1)
                for i in images
            ],
        )

    @activity.defn(name="preflight")
    async def _preflight(
        request: IngestDiveRequest, listing: DiveFolderListing
    ) -> IngestPreflight:
        calls.append("preflight")
        return IngestPreflight(
            dive_path=listing.folder_path,
            tenant_id=TENANT,
            images=images,
            resolved_device_id=DEVICE,
            errors=list(errors),
        )

    @activity.defn(name="create_dive")
    async def _create(
        request: IngestDiveRequest, preflight: IngestPreflight
    ) -> uuid.UUID:
        calls.append("create")
        return DIVE

    @activity.defn(name="scan_and_register")
    async def _scan(
        tenant_id: uuid.UUID, dive_id: uuid.UUID, paths: list[str],
        device_id: uuid.UUID | None,
    ) -> BatchResult:  # fmt: skip
        # v2: the tenant and device preflight resolved are what the scan uses.
        assert (tenant_id, dive_id, device_id) == (TENANT, DIVE, DEVICE)
        calls.append(f"scan:{len(paths)}")
        return BatchResult(
            registered=len(paths),
            rejected=list(rejected),
            max_taken_datetime=T,
        )

    @activity.defn(name="finalize_dive")
    async def _finalize(
        tenant_id: uuid.UUID, dive_id: uuid.UUID, request: IngestDiveRequest,
        totals: IngestTotals,
    ) -> IngestReport:  # fmt: skip
        assert tenant_id == TENANT
        calls.append("finalize")
        return IngestReport(
            dive_path=request.dive_path,
            dive_id=dive_id,
            total=totals.total,
            registered=totals.registered,
            skipped_existing=totals.skipped_existing,
            dive_datetime=totals.max_taken_datetime,
            committed=True,
        )

    return [_list, _preflight, _create, _scan, _finalize]


async def _run(request, **kwargs):
    calls: list[str] = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IngestDiveWorkflow],
            activities=_activities(calls, **kwargs),
        ):
            report = await env.client.execute_workflow(
                IngestDiveWorkflow.run,
                request,
                id=f"{TASK_QUEUE}-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
    return report, calls


def _request(**kwargs):
    kwargs.setdefault("tenant", "lab")
    kwargs.setdefault("dive_path", FOLDER)
    kwargs.setdefault("self_calibrates", True)
    return IngestDiveRequest(**kwargs)


# -- the happy path ------------------------------------------------------------


async def test_creates_the_dive_before_registering_and_finalizes_after():
    """The two-phase commit, as an ordering. `create` must precede every scan
    (frames need a dive to belong to) and `finalize` must follow all of them --
    it is the step that opens the commit flag."""
    report, calls = await _run(_request())

    assert calls[:3] == ["list", "preflight", "create"]
    assert calls[-1] == "finalize"
    assert report.committed is True
    assert report.dive_id == DIVE


async def test_frames_are_scanned_in_batches():
    """A failure should cost one batch of ~14.5 MB downloads, not a whole dive."""
    frames = BATCH_SIZE * 2 + 3
    _report, calls = await _run(_request(), frames=frames)

    scans = [c for c in calls if c.startswith("scan:")]
    assert scans == [f"scan:{BATCH_SIZE}", f"scan:{BATCH_SIZE}", "scan:3"]


async def test_batch_counts_accumulate_into_one_report():
    frames = BATCH_SIZE + 1
    report, _calls = await _run(_request(), frames=frames)

    assert report.total == frames
    assert report.registered == frames
    assert report.dive_datetime == T


# -- writing nothing -----------------------------------------------------------


async def test_a_dry_run_writes_nothing_and_returns_the_preflight():
    report, calls = await _run(_request(dry_run=True))

    assert calls == ["list", "preflight"]
    assert report.committed is False
    assert report.preflight is not None
    assert report.dive_id is None


async def test_a_failed_preflight_writes_nothing():
    """Preflight reports every fault at once; the workflow's job is simply not
    to proceed. No dive, no images -- the operator fixes and resubmits."""
    report, calls = await _run(_request(), errors=["FSL-07 has no intrinsics"])

    assert calls == ["list", "preflight"]
    assert report.committed is False
    assert report.preflight.errors == ["FSL-07 has no intrinsics"]


# -- the report carries the preflight ------------------------------------------


async def test_the_committed_report_still_carries_its_preflight():
    """Warnings -- a leaf-name collision, a subfolder that is really another
    dive, an Artist disagreeing with the resolved device -- live in the
    preflight. Dropping it on success would discard everything the operator was
    meant to see."""
    report, _calls = await _run(_request())

    assert report.preflight is not None
    assert report.preflight.resolved_device_id == DEVICE
