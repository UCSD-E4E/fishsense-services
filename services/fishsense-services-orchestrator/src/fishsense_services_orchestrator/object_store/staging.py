"""Stage a dive's raw frames from the NAS into Garage scratch.

Ported from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/stage_raw_bytes_for_dive_activity.py. Behavior is v1's:

* **canonical frames only** (the catalog's query), mirroring every cohort;
* **idempotent**: a frame already staged is HEAD-skipped, so a dive in several
  cohorts stages once and a retried firing is cheap;
* **the NAS is read-only** here: download, never delete;
* **no inner retry.** Retry and backoff belong to the bounded, jittered
  Temporal policy on the call (`steps.STAGE_RAW_RETRY_POLICY`); an inner loop
  under it produced the 200x-per-file storm that tripped the NAS auto-block
  (krg-infra#501). A transient FileStation error (502, 407, 402) propagates; a
  permanent one (408, no such file) becomes a non-retryable `NasFileNotFound`,
  and so does any failure for a file the NAS then says is not there (v2: a
  moved file's download is a 502, which retried forever), surfaced from under the TaskGroup's ExceptionGroup so Temporal honors it;
* **never skip a file** that fails to download: a silently missing frame
  would stage the dive incomplete and hide a real NAS problem;
* **one download at a time** by default: FileStation's shared download
  backend (DSM nginx -> synoscgi) 502s under concurrent large transfers.

**From the tenant's bucket first** (2026-10-21). The render stage copies each
raw into the tenant's bucket and records the copy (``capture_raws``, 0052,
checked against the capture's checksum). Mission 440's staging hit its hour
copying 1,201 raws from the NAS, every one already in the bucket. So a
capture with a copy in the tenant's current bucket is staged from there, its
bytes checked against the recorded size and MD5 first; a copy that is missing,
in another bucket, holds other bytes or can't be read falls back to the NAS,
logged. Bucket reads don't take the NAS's slots: they run `BUCKET_CONCURRENCY`
at a time beside them, a limit shared by every staging on the worker. Review
of #219: the copy must also be the capture's own (its MD5 is the capture's
checksum, the key it is staged under); a capture with a copy but no NAS path
is staged from the copy; the first read the bucket itself fails sends the
rest to the NAS with one warning; a capture already staged is checked once;
and a target naming captures stages only those.

v2 changes: activities are methods of a class given its catalog, object store
and NAS client factory; the target is (tenant, dive) and each frame is staged
under the tenant's key; the concurrency is ``FISHSENSE_NAS_STAGE_CONCURRENCY``,
read at startup, so a value that isn't a number fails the worker's start
rather than (as in v1) silently falling back to one.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from synology_filestation import FileStationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.raw_staging_store import RawCopy
from fishsense_services_orchestrator.ingest.nas import NasClient
from fishsense_services_orchestrator.ingest.nas_errors import (
    NAS_FILE_NOT_FOUND_TYPE,
    raise_if_permanent_dsm_error,
)
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
    resolve_nas_path,
)
from fishsense_services_orchestrator.object_store.catalog import (
    RawStagingCatalog,
    StagingCapture,
)
from fishsense_services_orchestrator.object_store.contracts import (
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.object_store.tenant_buckets import (
    tenant_objects,
)

__all__ = [
    "BUCKET_CONCURRENCY",
    "DEFAULT_STAGE_CONCURRENCY",
    "RawStagingActivities",
    "RawStagingSettings",
]

# One serial stream by default; ramp up (1 -> 2 -> 3) through the setting while
# watching the NAS, without a redeploy. v1 had 8, then 3, then 1.
DEFAULT_STAGE_CONCURRENCY = 1
#: Bucket copies read at once: Garage, not the NAS, so not its limit.
BUCKET_CONCURRENCY = 8


class RawStagingSettings(BaseSettings):
    """How hard staging may lean on the NAS, from ``FISHSENSE_NAS_*`` (the
    connection itself is `NasSettings`)."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_NAS_")

    stage_concurrency: int = DEFAULT_STAGE_CONCURRENCY

    @field_validator("stage_concurrency")
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        return max(1, value)


def _iter_leaf_exceptions(exc: BaseException):
    """The leaf (non-group) exceptions of a possibly nested ExceptionGroup, so
    a wrapped classification can be recovered."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_leaf_exceptions(sub)
    else:
        yield exc


async def _download_one(nas: NasClient, *, src_path: str, dest_dir: str) -> None:
    """One file, one attempt: only *classify* a failure (see module docstring).

    FileStation answers a download of a file that is not there (moved or
    deleted since ingest) with a 502, not a 408, so any failed download asks
    whether the file exists; only a definite no makes it permanent.
    """
    try:
        await asyncio.to_thread(nas.download_to, src_path=src_path, dest_dir=dest_dir)
    except FileStationError as exc:
        raise_if_permanent_dsm_error(exc, context=src_path)
        await _raise_if_missing(nas, src_path, exc)
        raise
    except Exception as exc:
        await _raise_if_missing(nas, src_path, exc)
        raise


async def _raise_if_missing(nas: NasClient, src_path: str, exc: Exception) -> None:
    try:
        there = await asyncio.to_thread(nas.exists, file_path=src_path)
    except Exception:  # pylint: disable=broad-exception-caught
        # A check that fails proves nothing missing: the download's error
        # stands, and the retry policy owns it.
        return
    if not there:
        raise ApplicationError(
            f"NAS path not found (download failed, and the file is not there): "
            f"{src_path}",
            type=NAS_FILE_NOT_FOUND_TYPE,
            non_retryable=True,
        ) from exc


class RawStagingActivities:
    """Raw staging and what it depends on."""

    def __init__(
        self,
        *,
        catalog: RawStagingCatalog,
        store: OrchestratorObjectStore,
        nas_settings: NasSettings,
        staging_settings: RawStagingSettings,
        nas_client_factory: Callable[[], NasClient] | None = None,
        stores: Any = None,
    ) -> None:
        self._catalog = catalog
        #: The tenant stores (`TenantStores`); None: everything from the NAS.
        self._stores = stores
        #: The worker's limit on bucket reads, made on first use (in the
        #: worker's event loop).
        self._bucket_slots: asyncio.Semaphore | None = None
        self._store = store
        self._nas_settings = nas_settings
        self._concurrency = staging_settings.stage_concurrency
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    async def _tenant_objects(self, tenant_id) -> Any:
        """The tenant's bucket, or None: no stores configured (no database
        round trip then), none for the tenant, or its slug not to be had --
        staging the NAS would have done must not fail for want of the bucket
        (review of #219)."""
        if self._stores is None or not self._stores.tenants:
            return None
        try:
            return await tenant_objects(self._catalog, self._stores, tenant_id)
        except Exception:  # pylint: disable=broad-exception-caught
            activity.logger.warning(
                "tenant=%s: no bucket to stage from; staging from the NAS",
                tenant_id, exc_info=True,
            )  # fmt: skip
            return None

    def _bucket_slots_for_worker(self) -> asyncio.Semaphore:
        """One limit on bucket reads for the whole worker: each holds a raw in
        memory (~15 MB), and several stagings run at once (review of #219)."""
        if self._bucket_slots is None:
            self._bucket_slots = asyncio.Semaphore(BUCKET_CONCURRENCY)
        return self._bucket_slots

    @staticmethod
    async def _read_copy(
        objects: Any, capture: StagingCapture
    ) -> tuple[bytes | None, str, bool]:
        """``(bytes, why not, the bucket itself failed)``: the capture's raw
        from its copy in `objects`' bucket, checked against what the copy
        recorded and against the capture's own checksum -- the key it is
        staged under (review of #219)."""
        copy: RawCopy | None = next(
            (c for c in capture.copies if c.bucket == objects.bucket), None
        )
        if copy is None:
            return None, "no copy in the tenant's bucket", False
        if copy.md5 != capture.checksum:
            return None, f"{copy.key} is not a copy of this capture", False
        try:
            data = await asyncio.to_thread(objects.get, copy.key)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return None, f"reading {copy.key} failed: {exc}", True
        if data is None:
            return None, f"{copy.key} is not there", False
        if len(data) != copy.bytes or hashlib.md5(data).hexdigest() != copy.md5:
            return None, f"{copy.key} is not the recorded copy", False
        return data, "", False

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage_raw_bytes_for_mission(
        self, target: StagingTarget
    ) -> StageRawBytesResult:
        captures = await self._catalog.captures_to_stage(
            target.tenant_id, target.mission_id, target.capture_ids
        )
        activity.logger.info(
            "staging raw bytes tenant=%s dive=%s images=%d concurrency=%d",
            target.tenant_id,
            target.mission_id,
            len(captures),
            self._concurrency,
        )

        nas = self._nas_client_factory()
        objects = await self._tenant_objects(target.tenant_id)
        sem = asyncio.Semaphore(self._concurrency)
        checks = asyncio.Semaphore(BUCKET_CONCURRENCY)
        bucket_slots = self._bucket_slots_for_worker()
        #: Cleared by the first read the bucket itself fails: a rotated key or
        #: a missing bucket fails every read alike, so the rest go to the NAS
        #: with one warning, not one apiece (review of #219).
        bucket_ok = objects is not None
        staged = skipped = no_path = from_bucket = 0

        async def _from_bucket(capture: StagingCapture) -> bool:
            """Staged from the tenant's bucket."""
            nonlocal staged, from_bucket, bucket_ok
            async with bucket_slots:
                if not bucket_ok:
                    return False
                data, why, broken = await self._read_copy(objects, capture)
                if data is None:
                    if broken and bucket_ok:
                        bucket_ok = False
                        activity.logger.warning(
                            "tenant=%s: the bucket failed (%s); staging the rest "
                            "from the NAS", target.tenant_id, why,
                        )  # fmt: skip
                    elif not broken:
                        activity.logger.warning(
                            "capture=%s: %s; staging it from the NAS",
                            capture.capture_id, why,
                        )  # fmt: skip
                    return False
                await self._store.upload_raw(target.tenant_id, capture.checksum, data)
            staged += 1
            from_bucket += 1
            activity.heartbeat()
            return True

        async def _stage_one(capture: StagingCapture) -> None:
            nonlocal staged, skipped, no_path
            if not capture.checksum:
                no_path += 1
                activity.heartbeat()
                return
            async with checks:
                present = await self._store.has_raw(target.tenant_id, capture.checksum)
            if present:
                skipped += 1
                activity.heartbeat()
                return
            # A copy in the bucket stages a capture whatever its NAS path, or
            # with none (review of #219).
            if bucket_ok and capture.copies and await _from_bucket(capture):
                return
            if not capture.source_path:
                no_path += 1
                activity.heartbeat()
                return

            async with sem:
                with tempfile.TemporaryDirectory() as tmpdir:
                    src_path = resolve_nas_path(capture.source_path, self._nas_settings)
                    await _download_one(nas, src_path=src_path, dest_dir=tmpdir)
                    # The NAS client lands the file at dest_dir/basename, and
                    # renames it there only once complete.
                    local = Path(tmpdir) / Path(src_path).name
                    data = await asyncio.to_thread(local.read_bytes)
                    await self._store.upload_raw(
                        target.tenant_id, capture.checksum, data
                    )
                staged += 1
                activity.heartbeat()

        try:
            async with asyncio.TaskGroup() as tg:
                for capture in captures:
                    tg.create_task(_stage_one(capture))
        except BaseExceptionGroup as group:
            # The group would hide a leaf's non_retryable flag from Temporal:
            # surface a permanent classification un-wrapped, so a doomed staging
            # isn't rescheduled; anything else re-raises for the bounded policy.
            for leaf in _iter_leaf_exceptions(group):
                if isinstance(leaf, ApplicationError) and leaf.non_retryable:
                    # `leaf` already carries its own cause (the NAS error).
                    raise leaf  # pylint: disable=raise-missing-from
            raise

        activity.logger.info(
            "staged tenant=%s dive=%s staged=%d (from the bucket %d) skipped=%d "
            "no_path=%d",
            target.tenant_id,
            target.mission_id,
            staged,
            from_bucket,
            skipped,
            no_path,
        )
        return StageRawBytesResult(
            staged=staged,
            skipped_already_present=skipped,
            no_path=no_path,
            from_bucket=from_bucket,
        )
