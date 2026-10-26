"""The database side of the focal-length backfill, tenant-scoped.

New in v2 (2026-10-25). The zoom gate needs each frame's EXIF focal length
(migration 0069); ingest records it from now on, and the frames ingested
before are read back from the NAS by the orchestrator (``ops.focal_lengths``,
4 KB a frame). This store is what it reads and the one write it makes.

The write fills an unknown focal length only, so a re-run is a no-op for
frames already read and can never overwrite what ingest recorded.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "FocalLengthCatalog",
    "FocalLengthFrame",
    "FocalLengthMission",
    "frames_without_focal_length",
    "missions_without_focal_lengths",
    "record_focal_length",
]

#: A frame still to read: on the NAS (the object store holds phone captures,
#: which carry no zoom) and not yet read.
_TO_READ = "source_path IS NOT NULL AND focal_length_mm IS NULL"


@dataclass(frozen=True)
class FocalLengthFrame:
    id: uuid.UUID
    #: Share-relative NAS path (v1's convention).
    source_path: str


@dataclass(frozen=True)
class FocalLengthMission:
    id: uuid.UUID
    number: int
    #: Frames still to read.
    frames: int


async def missions_without_focal_lengths(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[FocalLengthMission]:
    """The missions with frames still to read, in number order."""
    rows = await conn.execute(
        text("""
            SELECT m.id, m.number, count(*) AS frames
            FROM captures c JOIN missions m ON m.id = c.mission_id
            WHERE c.tenant_id = :tenant
              AND c.source_path IS NOT NULL AND c.focal_length_mm IS NULL
            GROUP BY m.id, m.number
            ORDER BY m.number
            """),
        {"tenant": tenant_id},
    )
    return [FocalLengthMission(r.id, r.number, r.frames) for r in rows]


async def frames_without_focal_length(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> list[FocalLengthFrame]:
    """The mission's frames still to read, duplicates included, in path order."""
    rows = await conn.execute(
        text(f"""
            SELECT id, source_path FROM captures
            WHERE tenant_id = :tenant AND mission_id = :mission AND {_TO_READ}
            ORDER BY source_path
            """),
        {"tenant": tenant_id, "mission": mission_id},
    )
    return [FocalLengthFrame(r.id, r.source_path) for r in rows]


async def record_focal_length(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    focal_length_mm: float,
) -> bool:
    """Record the frame's focal length if it has none. Whether it did."""
    written = (
        await conn.execute(
            text("""
                UPDATE captures SET focal_length_mm = :focal
                WHERE tenant_id = :tenant AND id = :id AND focal_length_mm IS NULL
                RETURNING id
                """),
            {"tenant": tenant_id, "id": capture_id, "focal": focal_length_mm},
        )
    ).scalar_one_or_none()
    return written is not None


class FocalLengthCatalog(ServicePrincipal):
    """The focal-length backfill's database side, as the orchestrator's
    principal."""

    async def missions_without_focal_lengths(
        self, tenant_id: uuid.UUID
    ) -> list[FocalLengthMission]:
        async with self._tenant(tenant_id) as conn:
            return await missions_without_focal_lengths(conn, tenant_id)

    async def frames_without_focal_length(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> list[FocalLengthFrame]:
        async with self._tenant(tenant_id) as conn:
            return await frames_without_focal_length(conn, tenant_id, mission_id)

    async def record_focal_length(
        self, tenant_id: uuid.UUID, capture_id: uuid.UUID, focal_length_mm: float
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await record_focal_length(
                conn, tenant_id, capture_id, focal_length_mm
            )
