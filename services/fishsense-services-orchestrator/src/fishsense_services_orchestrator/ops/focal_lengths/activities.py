"""Read each NAS frame's EXIF focal length and (with ``apply``) record it.

New in v2 (2026-10-25). The TG-6 zooms 4.5-18 mm and its intrinsics are
calibrated at 4.5; a zoomed frame's laser dots sit on another line and its
depth is wrong (missions 374, 384, 386). Ingest records the focal length from
now on (migration 0069); this reads it back for the frames ingested before.

The rules:

* **a dry run unless told otherwise**: `apply=False` reads and reports, and
  writes nothing -- the dry run is the corpus count the gate is proven on;
* **4 KB a frame** (`FOCAL_HEADER_BYTES`): the tag sits at byte ~2,870 of a
  TG-6 raw, and the NAS moves ~1 MB/s; never the whole file. A TG-7 puts its
  Exif IFD further in (missions 480-487, all 484 frames unreadable at 4 KB on
  the 2026-10-25 apply; found at 64 KB), so a frame with no FocalLength in
  4 KB is read again to `FOCAL_RETRY_BYTES` before it is unreadable;
* the write fills an unknown focal length only (`record_focal_length`), so a
  re-run skips what was read and a value written meanwhile is counted, not
  overwritten;
* a frame not on the NAS is a finding (``RepairMovedCapturePathsWorkflow``
  repairs moved paths); any other NAS error is an outage and propagates for
  the retry policy.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Protocol

from synology_filestation import FileStationError
from temporalio import activity

from fishsense_services_api.focal_length_store import (
    FocalLengthFrame,
    FocalLengthMission,
)
from fishsense_services_orchestrator.ingest.exif import read_exif
from fishsense_services_orchestrator.ingest.nas import NasClient
from fishsense_services_orchestrator.ingest.nas_errors import is_nas_not_found
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
    resolve_nas_path,
)
from fishsense_services_orchestrator.ops.focal_lengths.contracts import (
    LISTED,
    FocalLengthMissionRef,
    FocalLengthMissionReport,
)

__all__ = [
    "FOCAL_HEADER_BYTES",
    "FOCAL_RETRY_BYTES",
    "FocalLengthActivities",
    "FocalLengthCatalog",
    "WIDE_END_MM",
]

#: Enough of a TG-6 raw for its FocalLength (at byte 2,868 on the fixture).
FOCAL_HEADER_BYTES = 4096
#: Enough of a TG-7 raw: read only when the first 4 KB has no FocalLength.
FOCAL_RETRY_BYTES = 65536
#: The TG-6's wide end, where its intrinsics are calibrated.
WIDE_END_MM = 4.5


class FocalLengthCatalog(Protocol):
    """See ``fishsense_services_api.focal_length_store.FocalLengthCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def missions_without_focal_lengths(
        self, tenant_id: uuid.UUID
    ) -> list[FocalLengthMission]: ...

    async def frames_without_focal_length(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> list[FocalLengthFrame]: ...

    async def record_focal_length(
        self, tenant_id: uuid.UUID, capture_id: uuid.UUID, focal_length_mm: float
    ) -> bool: ...


class FocalLengthActivities:
    def __init__(
        self,
        *,
        catalog: FocalLengthCatalog,
        nas_settings: NasSettings,
        nas_client_factory: Callable[[], NasClient] | None = None,
    ) -> None:
        self._catalog = catalog
        self._nas_settings = nas_settings
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    @activity.defn(name="focal_length_missions")
    async def focal_length_missions(self) -> list[FocalLengthMissionRef]:
        """Every mission, in every tenant served, with frames still to read."""
        refs = []
        for tenant_id in await self._catalog.member_tenants():
            for mission in await self._catalog.missions_without_focal_lengths(
                tenant_id
            ):
                refs.append(
                    FocalLengthMissionRef(
                        tenant_id=str(tenant_id),
                        mission_id=str(mission.id),
                        mission_number=mission.number,
                        frames=mission.frames,
                    )
                )
        return refs

    @activity.defn(name="read_mission_focal_lengths")
    async def read_mission_focal_lengths(
        self, mission: FocalLengthMissionRef, apply: bool = False
    ) -> FocalLengthMissionReport:
        """Read the mission's frames' focal lengths; with ``apply``, record
        each one read."""
        tenant_id = uuid.UUID(mission.tenant_id)
        frames = await self._catalog.frames_without_focal_length(
            tenant_id, uuid.UUID(mission.mission_id)
        )
        report = FocalLengthMissionReport(
            mission_number=mission.mission_number, applied=apply, frames=len(frames)
        )
        nas = await asyncio.to_thread(self._nas_client_factory)
        counts: Counter[str] = Counter()
        for frame in frames:
            focal = await self._read(nas, frame, report)
            if focal is not None:
                counts[f"{focal:.2f}"] += 1
                if abs(focal - WIDE_END_MM) > 0.005:
                    report.zoomed += 1
                if apply:
                    if await self._catalog.record_focal_length(
                        tenant_id, frame.id, focal
                    ):
                        report.recorded += 1
                    else:
                        report.already_known += 1
            activity.heartbeat()
        report.focal_lengths = dict(sorted(counts.items(), key=lambda kv: float(kv[0])))

        activity.logger.info(
            "focal lengths mission=%d applied=%s frames=%d zoomed=%d "
            "unreadable=%d not_found=%d recorded=%d",
            mission.mission_number,
            apply,
            report.frames,
            report.zoomed,
            report.unreadable_count,
            report.not_found_count,
            report.recorded,
        )
        return report

    async def _read(
        self, nas: NasClient, frame: FocalLengthFrame, report: FocalLengthMissionReport
    ) -> float | None:
        path = resolve_nas_path(frame.source_path, self._nas_settings)
        focal = None
        for length in (FOCAL_HEADER_BYTES, FOCAL_RETRY_BYTES):
            try:
                header = await asyncio.to_thread(
                    nas.download_range, file_path=path, offset=0, length=length
                )
            except FileStationError as exc:
                if not is_nas_not_found(exc):
                    raise
                report.not_found_count += 1
                if len(report.not_found) < LISTED:
                    report.not_found.append(frame.source_path)
                return None
            focal = read_exif(header).focal_length_mm
            if focal is not None:
                break
        if focal is None:
            report.unreadable_count += 1
            if len(report.unreadable) < LISTED:
                report.unreadable.append(frame.source_path)
        return focal
