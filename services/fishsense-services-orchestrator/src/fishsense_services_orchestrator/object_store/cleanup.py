"""Delete a dive's staged raw frames from Garage scratch -- only scratch, and
only once nothing is reading it.

Ported from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/cleanup_raw_bytes_for_dive_activity.py. Behaviour is v1's:

* runs after the processor has written the JPEGs, and drops only the
  reproducible-from-NAS raw scratch; the JPEGs stay (Label Studio presigns
  them). **NAS safety**: this module holds no NAS client (a tripwire test says
  so), and the only deletes it can issue are of scratch keys;
* **skipped while a sibling raw reader is Running** (prod dive 442,
  2026-09-07: a species cleanup deleted 984 objects under a laser render). The
  child-id sentinel can't cover it, because the ids differ across stages; the
  gate lives here rather than at each call site because it is one place, it
  covers every caller, and it adds no workflow command. Whichever stage
  finishes last cleans up;
* **fails closed**: if Temporal can't be asked, the scratch is "in use".
  Deleting under a live child kills a render silently and costs the dive's
  whole NAS staging to redo; keeping it costs space until the next firing,
  which re-stages cheaply (`skipped_already_present`);
* deletes are idempotent, so a retry is safe; up to 8 run at once.

v2 changes: the target is (tenant, dive), and only the tenant's keys are
deleted; the checksums are the catalog's, which leaves out scratch another dive
owns (`fishsense_services_api.raw_staging_store.checksums_to_clean`); Temporal
is asked through the worker's own client (`activity.client()`), not a second
connection per call; reader ids come from `readers`.
"""

from __future__ import annotations

import asyncio
import uuid

from temporalio import activity

from fishsense_services_orchestrator.object_store.catalog import RawStagingCatalog
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.readers import (
    RAW_SCRATCH_READERS,
    build_scratch_in_use_query,
    raw_scratch_reader_id,
    raw_scratch_reader_ids,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

__all__ = [
    "CLEANUP_CONCURRENCY",
    "RAW_SCRATCH_READERS",
    "RawCleanupActivities",
    "build_scratch_in_use_query",
    "raw_scratch_reader_id",
    "raw_scratch_reader_ids",
    "scratch_in_use",
]

CLEANUP_CONCURRENCY = 8

# What `scratch_in_use` answers when it cannot ask: a holder, never "free".
_UNKNOWN_HOLDER = "<temporal-unreachable>"


async def scratch_in_use(dive_id: uuid.UUID) -> str | None:
    """The id of a still-running raw reader of this dive, or None if the
    scratch is free. Any failure to ask is an answer of "in use"."""
    try:
        client = activity.client()
        async for execution in client.list_workflows(
            query=build_scratch_in_use_query(dive_id)
        ):
            return execution.id
    except Exception as exc:  # pylint: disable=broad-except
        activity.logger.warning(
            "cannot determine whether dive=%s scratch is in use (%s); "
            "declining to delete",
            dive_id,
            type(exc).__name__,
        )
        return _UNKNOWN_HOLDER
    return None


class RawCleanupActivities:
    """Raw scratch cleanup and what it depends on -- deliberately no NAS."""

    def __init__(
        self, *, catalog: RawStagingCatalog, store: OrchestratorObjectStore
    ) -> None:
        self._catalog = catalog
        self._store = store

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def cleanup_raw_bytes_for_dive(
        self, target: StagingTarget
    ) -> CleanupRawBytesResult:
        holder = await scratch_in_use(target.dive_id)
        if holder is not None:
            activity.logger.info(
                "skipping raw cleanup dive=%s: %s is still reading the scratch",
                target.dive_id,
                holder,
            )
            return CleanupRawBytesResult(deleted=0)

        checksums = await self._catalog.checksums_to_clean(
            target.tenant_id, target.dive_id
        )
        activity.logger.info(
            "cleaning up raw bytes tenant=%s dive=%s objects=%d",
            target.tenant_id,
            target.dive_id,
            len(checksums),
        )

        sem = asyncio.Semaphore(CLEANUP_CONCURRENCY)
        deleted = 0

        async def _delete_one(checksum: str) -> None:
            nonlocal deleted
            async with sem:
                if await self._store.delete_raw(target.tenant_id, checksum):
                    deleted += 1
                activity.heartbeat()

        async with asyncio.TaskGroup() as tg:
            for checksum in checksums:
                tg.create_task(_delete_one(checksum))

        activity.logger.info(
            "raw cleanup done dive=%s deleted=%d", target.dive_id, deleted
        )
        return CleanupRawBytesResult(deleted=deleted)
