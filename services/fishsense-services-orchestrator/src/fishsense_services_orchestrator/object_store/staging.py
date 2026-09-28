"""Stage a dive's raw frames from the NAS into Garage scratch.

Ported from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/stage_raw_bytes_for_dive_activity.py. Behaviour is v1's:

* **canonical frames only** (the catalog's query), mirroring every cohort;
* **idempotent**: a frame already staged is HEAD-skipped, so a dive in several
  cohorts stages once and a retried firing is cheap;
* **the NAS is read-only** here: download, never delete;
* **no inner retry.** Retry and backoff belong to the bounded, jittered
  Temporal policy on the call (`steps.STAGE_RAW_RETRY_POLICY`); an inner loop
  under it produced the 200x-per-file storm that tripped the NAS auto-block
  (krg-infra#501). A transient FileStation error (502, 407, 402) propagates; a
  permanent one (408, no such file) becomes a non-retryable `NasFileNotFound`,
  surfaced from under the TaskGroup's ExceptionGroup so Temporal honours it;
* **never skip a file** that fails to download: a silently missing frame
  would stage the dive incomplete and hide a real NAS problem;
* **one download at a time** by default: FileStation's shared download
  backend (DSM nginx -> synoscgi) 502s under concurrent large transfers.

v2 changes: activities are methods of a class given its catalog, object store
and NAS client factory; the target is (tenant, dive) and each frame is staged
under the tenant's key; the concurrency is ``FISHSENSE_NAS_STAGE_CONCURRENCY``,
read at startup, so a value that isn't a number fails the worker's start
rather than (as in v1) silently falling back to one.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from synology_filestation import DSMError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_orchestrator.ingest.nas import NasClient
from fishsense_services_orchestrator.ingest.nas_errors import (
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

__all__ = ["DEFAULT_STAGE_CONCURRENCY", "RawStagingActivities", "RawStagingSettings"]

# One serial stream by default; ramp up (1 -> 2 -> 3) through the setting while
# watching the NAS, without a redeploy. v1 had 8, then 3, then 1.
DEFAULT_STAGE_CONCURRENCY = 1


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
    """One file, one attempt: only *classify* a failure (see module docstring)."""
    try:
        await asyncio.to_thread(nas.download_to, src_path=src_path, dest_dir=dest_dir)
    except DSMError as exc:
        raise_if_permanent_dsm_error(exc, context=src_path)
        raise


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
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._nas_settings = nas_settings
        self._concurrency = staging_settings.stage_concurrency
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage_raw_bytes_for_dive(
        self, target: StagingTarget
    ) -> StageRawBytesResult:
        captures = await self._catalog.captures_to_stage(
            target.tenant_id, target.dive_id
        )
        activity.logger.info(
            "staging raw bytes tenant=%s dive=%s images=%d concurrency=%d",
            target.tenant_id,
            target.dive_id,
            len(captures),
            self._concurrency,
        )

        nas = self._nas_client_factory()
        sem = asyncio.Semaphore(self._concurrency)
        staged = skipped = no_path = 0

        async def _stage_one(capture: StagingCapture) -> None:
            nonlocal staged, skipped, no_path
            if not capture.source_path or not capture.checksum:
                no_path += 1
                activity.heartbeat()
                return

            async with sem:
                if await self._store.has_raw(target.tenant_id, capture.checksum):
                    skipped += 1
                    activity.heartbeat()
                    return

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
                    # `leaf` already carries its own cause (the DSMError).
                    raise leaf  # pylint: disable=raise-missing-from
            raise

        activity.logger.info(
            "staged tenant=%s dive=%s staged=%d skipped=%d no_path=%d",
            target.tenant_id,
            target.dive_id,
            staged,
            skipped,
            no_path,
        )
        return StageRawBytesResult(
            staged=staged, skipped_already_present=skipped, no_path=no_path
        )
