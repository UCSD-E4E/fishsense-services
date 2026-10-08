"""The database side of path repair, tenant-scoped.

New in v2 (2026-10-07). Frames were moved into subfolders on the NAS after
ingest, so their rows named files that no longer existed, and every stage that
staged one failed. The orchestrator (``ops.paths``) finds each moved frame on
the NAS and has the NAS hash it; this store is what it reads and the one write
it makes.

The write is narrow on purpose: a re-point names the row, the path it was
checked at and the checksum the NAS confirmed, and changes nothing unless all
three still hold. So it can only ever move the row that was verified, and a
re-run is a no-op for rows already moved.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.checksum_store import dive_by_number
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "CapturePathCatalog",
    "PathCapture",
    "captures_with_paths",
    "path_holder",
    "repoint_capture",
]


@dataclass(frozen=True)
class PathCapture:
    """One frame of a dive, as path repair looks for its file."""

    id: uuid.UUID
    #: v1's image id, for a migrated capture.
    number: int
    #: Share-relative NAS path (v1's convention).
    source_path: str
    checksum: str
    checksum_algorithm: str
    is_canonical: bool


async def captures_with_paths(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[PathCapture]:
    """Every capture of the dive with a NAS path, duplicates included, in path
    order. A frame held only in the object store has nothing to repair."""
    rows = await conn.execute(
        text("""
            SELECT id, number, source_path, checksum, checksum_algorithm,
                   is_canonical
            FROM captures
            WHERE tenant_id = :tenant AND dive_id = :dive
              AND source_path IS NOT NULL
            ORDER BY source_path, number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [
        PathCapture(
            r.id,
            r.number,
            r.source_path,
            r.checksum,
            r.checksum_algorithm,
            r.is_canonical,
        )
        for r in rows
    ]


async def path_holder(
    conn: AsyncConnection, tenant_id: uuid.UUID, path: str
) -> uuid.UUID | None:
    """The tenant's capture at ``path``, if any: paths are unique per tenant,
    so a repair onto a held path is reported, not attempted."""
    return (
        await conn.execute(
            text(
                "SELECT id FROM captures "
                "WHERE tenant_id = :tenant AND source_path = :path"
            ),
            {"tenant": tenant_id, "path": path},
        )
    ).scalar_one_or_none()


async def repoint_capture(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    *,
    old_path: str,
    new_path: str,
    checksum: str,
) -> bool:
    """Point the capture at ``new_path`` -- only if it is still at
    ``old_path`` with ``checksum``, the row that was checked. Whether it
    moved."""
    moved = (
        await conn.execute(
            text("""
                UPDATE captures SET source_path = :new
                WHERE tenant_id = :tenant AND id = :id
                  AND source_path = :old AND checksum = :checksum
                RETURNING id
                """),
            {
                "tenant": tenant_id,
                "id": capture_id,
                "old": old_path,
                "new": new_path,
                "checksum": checksum,
            },
        )
    ).scalar_one_or_none()
    return moved is not None


class CapturePathCatalog(ServicePrincipal):
    """Path repair's database side, as the orchestrator's principal."""

    async def dive_by_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None:
        async with self._tenant(tenant_id) as conn:
            return await dive_by_number(conn, tenant_id, number)

    async def captures_with_paths(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[PathCapture]:
        async with self._tenant(tenant_id) as conn:
            return await captures_with_paths(conn, tenant_id, dive_id)

    async def path_holder(self, tenant_id: uuid.UUID, path: str) -> uuid.UUID | None:
        async with self._tenant(tenant_id) as conn:
            return await path_holder(conn, tenant_id, path)

    async def repoint_capture(
        self,
        tenant_id: uuid.UUID,
        capture_id: uuid.UUID,
        *,
        old_path: str,
        new_path: str,
        checksum: str,
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await repoint_capture(
                conn,
                tenant_id,
                capture_id,
                old_path=old_path,
                new_path=new_path,
                checksum=checksum,
            )
