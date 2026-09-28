"""Re-hash a dive's captures against the NAS and report. Read-only.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/verify_dive_checksums_activity.py and
select_canonical_dive_ids_activity.py.

Answers "do we trust the existing data?" with a measurement instead of an
argument. Every ingest convention was recovered by reading the retired spider
crawler: the checksum is ``md5`` of the whole file (``backend.py:67``) and the
capture time is naive EXIF 0x0132 stamped UTC (``backend.py:20``). That proves
what *spider* wrote, not that every row came from spider, and the failure mode
is silent: checksums computed differently make duplicate detection report
**zero overlap**, so a re-ingest would land every frame canonical.

v1's rules, kept:

* **read-only, by construction.** No NAS writes, no database writes; a test
  tripwire asserts this module's source contains no write call;
* **findings, not failures.** A mismatch, or a row whose file is gone (408), is
  one of the answers being looked for, so the run records it and carries on.
  Any other NAS error (an outage, not an absence) propagates for Temporal's
  bounded policy -- the opposite of staging, which is trying to *do* something;
* **not canonical-filtered**: the duplicates are the least-trusted rows;
* **whole files**, one at a time, in path order; ``limit`` samples the first N.

v2 changes:

* the dive is named by its ``number`` and found in whichever tenant the
  orchestrator serves holds it; an unknown number is a non-retryable
  ``DiveNotFound`` (v1 returned an empty report, so a typo read as clean);
* **a retry resumes.** The progress (the next index and the report so far) is
  heartbeated after every frame and read back on a retry. v1 heartbeated the
  index "so a retry resumes" but never read it, so every retry re-downloaded
  the whole sample;
* a capture recorded with ``sha256`` is hashed with sha256 (v2 records the
  algorithm; v1's column was md5 by convention);
* the sweep's dives come from every served tenant, in number order.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Protocol

from synology_filestation import DSMError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.checksum_store import VerifyCapture
from fishsense_services_orchestrator.ingest.nas import NasClient
from fishsense_services_orchestrator.ingest.nas_errors import dsm_error_code
from fishsense_services_orchestrator.ingest.nas_frames import (
    HASH_CHUNK_BYTES,
    NasSettings,
    build_nas_client,
    read_taken_datetime,
    resolve_nas_path,
)
from fishsense_services_orchestrator.ops.checksums.contracts import (
    ChecksumMismatch,
    VerifyChecksumsReport,
)

__all__ = ["ChecksumActivities", "ChecksumCatalog", "DIVE_NOT_FOUND_TYPE"]

#: "No such file or directory". Here a *finding* (the row outlived its file),
#: not the permanent failure it is when staging.
_DSM_NOT_FOUND = 408

#: The non-retryable error for a dive number no served tenant holds.
DIVE_NOT_FOUND_TYPE = "DiveNotFound"


class ChecksumCatalog(Protocol):
    """See ``fishsense_services_api.checksum_store.ChecksumCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def dive_by_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None: ...

    async def captures_to_verify(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[VerifyCapture]: ...

    async def canonical_dive_numbers(self, tenant_id: uuid.UUID) -> list[int]: ...


def _file_digest(path: Path, algorithm: str) -> str:
    """The whole file, streamed in spider's chunks (``backend.py:67``), under
    the capture's recorded algorithm: md5 is the convention of record."""
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for blob in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(blob)
    return digest.hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    """Compare like with like: a naive time is UTC by the migration's
    construction, and an aware one in another zone is the same instant."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _verify_one(
    nas: NasClient,
    capture: VerifyCapture,
    settings: NasSettings,
    report: VerifyChecksumsReport,
) -> None:
    """Download one frame, compare, and note the outcome. Never raises on a
    finding."""
    src_path = resolve_nas_path(capture.source_path, settings)
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            nas.download_to(src_path=src_path, dest_dir=tmpdir)
        except DSMError as exc:
            if dsm_error_code(exc) == _DSM_NOT_FOUND:
                report.missing_on_nas.append(
                    ChecksumMismatch(
                        capture_number=capture.number,
                        path=capture.source_path,
                        stored=capture.checksum,
                    )
                )
                return
            # A NAS that is down, rather than a file that is absent: propagate
            # so Temporal's bounded policy retries, rather than noting a
            # finding that is really an outage.
            raise

        local = Path(tmpdir) / os.path.basename(src_path)
        computed = _file_digest(local, capture.checksum_algorithm)
        exif_taken = read_taken_datetime(local)

    if not capture.checksum:
        report.no_stored_checksum.append(
            ChecksumMismatch(
                capture_number=capture.number,
                path=capture.source_path,
                computed=computed,
            )
        )
    elif capture.checksum == computed:
        report.checksum_matched += 1
    else:
        report.mismatches.append(
            ChecksumMismatch(
                capture_number=capture.number,
                path=capture.source_path,
                stored=capture.checksum,
                computed=computed,
            )
        )

    stored_taken = _as_utc(capture.captured_at)
    if stored_taken != exif_taken:
        report.timestamp_mismatches.append(
            ChecksumMismatch(
                capture_number=capture.number,
                path=capture.source_path,
                stored=stored_taken.isoformat() if stored_taken else None,
                computed=exif_taken.isoformat() if exif_taken else None,
            )
        )


def _resumed(dive_number: int) -> tuple[int, VerifyChecksumsReport | None]:
    """Where the last attempt got to: the next index and its report so far,
    from its last heartbeat; (0, None) on a first attempt."""
    details = activity.info().heartbeat_details
    if not details:
        return 0, None
    progress = details[0]
    report = VerifyChecksumsReport.model_validate(progress["report"])
    if report.dive_number != dive_number:
        return 0, None
    return int(progress["next"]), report


def _beat(next_index: int, report: VerifyChecksumsReport) -> None:
    activity.heartbeat({"next": next_index, "report": report.model_dump(mode="json")})


class ChecksumActivities:
    def __init__(
        self,
        *,
        catalog: ChecksumCatalog,
        nas_settings: NasSettings,
        nas_client_factory: Callable[[], NasClient] | None = None,
    ) -> None:
        self._catalog = catalog
        self._nas_settings = nas_settings
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    async def _find_dive(self, dive_number: int) -> tuple[uuid.UUID, uuid.UUID]:
        for tenant_id in await self._catalog.member_tenants():
            dive_id = await self._catalog.dive_by_number(tenant_id, dive_number)
            if dive_id is not None:
                return tenant_id, dive_id
        raise ApplicationError(
            f"no dive numbered {dive_number} in any tenant the orchestrator serves",
            type=DIVE_NOT_FOUND_TYPE,
            non_retryable=True,
        )

    @activity.defn(name="verify_dive_checksums")
    async def verify_dive_checksums(
        self, dive_number: int, limit: Optional[int] = None
    ) -> VerifyChecksumsReport:
        """Re-hash the dive's captures (the first ``limit`` in path order, or
        all) and report every disagreement."""
        tenant_id, dive_id = await self._find_dive(dive_number)
        captures = await self._catalog.captures_to_verify(tenant_id, dive_id)
        selected = captures if limit is None else captures[:limit]

        start, report = _resumed(dive_number)
        if report is None:
            report = VerifyChecksumsReport(
                dive_number=dive_number, total_in_dive=len(captures)
            )
        else:
            activity.logger.info(
                "resuming dive=%d at frame %d of %d", dive_number, start, len(selected)
            )

        nas = self._nas_client_factory()
        _beat(start, report)
        for index in range(start, len(selected)):
            capture = selected[index]
            if capture.source_path:
                await asyncio.to_thread(
                    _verify_one, nas, capture, self._nas_settings, report
                )
                report.checked += 1
            _beat(index + 1, report)

        activity.logger.info(
            "verified dive=%d checked=%d matched=%d mismatched=%d "
            "timestamp_mismatched=%d missing=%d no_checksum=%d",
            dive_number,
            report.checked,
            report.checksum_matched,
            len(report.mismatches),
            len(report.timestamp_mismatches),
            len(report.missing_on_nas),
            len(report.no_stored_checksum),
        )
        return report

    @activity.defn(name="select_canonical_dive_numbers")
    async def select_canonical_dive_numbers(self) -> List[int]:
        """Every served tenant's dives with at least one canonical capture, in
        number order (v1's ``GET /api/v1/canonical/dives/``, sorted)."""
        numbers = [
            number
            for tenant_id in await self._catalog.member_tenants()
            for number in await self._catalog.canonical_dive_numbers(tenant_id)
        ]
        return sorted(numbers)
