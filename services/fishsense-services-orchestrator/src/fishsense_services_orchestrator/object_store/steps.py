"""The staging and cleanup steps a parent workflow takes, with v1's timeouts.

Ported from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
workflows/_dispatch.py (`stage_raw`, `cleanup_raw`) and _retry_policies.py
(`STAGE_RAW_RETRY_POLICY`). Workflow code: import it inside a parent's
``workflow.unsafe.imports_passed_through()`` like the other contracts.

The shape every raw-reading parent follows (v1's)::

    await stage_raw(target)        # fatal on failure: a child that would 404
                                   # on every frame wastes a whole fan-out
    ... dispatch the child with an id from `raw_scratch_reader_id` ...
    await cleanup_raw(target)      # skipped by the activity while any
                                   # sibling raw reader is still running

v2 change: the target is (tenant, dive).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)

__all__ = [
    "CLEANUP_RAW_RETRY_POLICY",
    "LONG_STAGE_RAW_TIMEOUT",
    "STAGE_RAW_RETRY_POLICY",
    "STAGE_RAW_TIMEOUT",
    "cleanup_raw",
    "stage_raw",
]

# Raw staging pulls `.ORF`s from FileStation, whose shared download backend 502s
# under concurrent large-file load. A 502 should self-heal on a *bounded,
# backed-off* reschedule -- never an inner loop under this policy, which
# produced the 200x-per-file storm that tripped the NAS auto-block
# (krg-infra#501). Capped so a persistent outage fails the firing in about a
# minute and the hourly schedule owns trying again; Temporal adds jitter.
# `NasFileNotFound` (Synology 408) is non-retryable: a missing frame won't
# appear by retrying. The string must match `ingest.nas_errors`.
STAGE_RAW_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=5,
    non_retryable_error_types=["NasFileNotFound", "NotAMember"],
)


#: Staging's budget by default: v1's hour. Most parents run under an hour's
#: run timeout of their own (laser calibration, 30 minutes), so a longer
#: staging would end in their run's timeout -- with no cleanup -- not in a
#: failure they clean up after (review of #219).
STAGE_RAW_TIMEOUT = timedelta(hours=1)
#: For a parent whose own budget covers it (the automatic run, the render
#: mission). An hour did not cover a large mission from the NAS: mission 440's
#: 1,201 raws timed out at it (2026-10-21), with three missions staging at
#: once. The activity heartbeats per file (5 minutes), so a stalled staging is
#: still caught in minutes; this only has to cover a large mission's honest
#: pace. The parent's run timeout must sum it.
LONG_STAGE_RAW_TIMEOUT = timedelta(hours=6)


#: v1 gave cleanup no retry policy: retry within its window. A revoked
#: membership (`NotAMember`) is the exception -- no retry can fix it.
CLEANUP_RAW_RETRY_POLICY = RetryPolicy(non_retryable_error_types=["NotAMember"])


async def stage_raw(
    target: StagingTarget, *, timeout: timedelta = STAGE_RAW_TIMEOUT
) -> StageRawBytesResult:
    """Copy the dive's canonical raw frames into Garage scratch: from the
    tenant's bucket where the render stage copied them, else from the NAS.
    Cheap to repeat: frames already staged are HEAD-skipped."""
    return await workflow.execute_activity(
        "stage_raw_bytes_for_dive",
        target,
        schedule_to_close_timeout=timeout,
        heartbeat_timeout=timedelta(minutes=5),
        retry_policy=STAGE_RAW_RETRY_POLICY,
        result_type=StageRawBytesResult,
    )


async def cleanup_raw(target: StagingTarget) -> CleanupRawBytesResult:
    """Evict the dive's raw scratch. The JPEGs stay (Label Studio presigns
    them); the NAS is never touched."""
    return await workflow.execute_activity(
        "cleanup_raw_bytes_for_dive",
        target,
        schedule_to_close_timeout=timedelta(minutes=15),
        heartbeat_timeout=timedelta(minutes=5),
        retry_policy=CLEANUP_RAW_RETRY_POLICY,
        result_type=CleanupRawBytesResult,
    )
