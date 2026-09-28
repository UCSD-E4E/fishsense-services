"""The database side of raw staging and its cleanup, tenant-scoped.

Ported from fishsense-lite@77e8f8e5: v1's staging and cleanup activities
(services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/stage_raw_bytes_for_dive_activity.py and
cleanup_raw_bytes_for_dive_activity.py) both read every image row of the dive
through the SDK and filtered client-side; here the filters are queries. And
the processed-JPEG check reads a capture's checksum, as v1's populate did.

v1's rules, kept:

* **staging is canonical-only.** The same frame lives under several dives (half
  of v1's image rows are duplicates) and ``is_canonical`` marks the real copy.
  Every cohort gates on it, so staging must too, or the work dispatched would
  not match what the cohort promised and the dive could never drain;
* a frame with no NAS path is returned anyway, for staging to count rather
  than silently drop.

v2 changes:

* per tenant, as the orchestrator's service principal;
* **cleanup deletes only scratch this dive owns**: its checksums, except those
  whose canonical copy is in *another* dive. Scratch is keyed by checksum, so a
  duplicate shares its key with a canonical twin another dive staged -- v1
  deleted it anyway (to evict scratch staged before the canonical gate existed,
  of which v2's new ``tenants/`` keys have none), under a child of that other
  dive that the scratch-in-use gate, looking only at this dive's children,
  could not see;
* **a capture says whether it came from v1** (it has a ``v1_id``), so the
  processed-JPEG check may find the JPEG v1 wrote for it -- and never for a
  frame v2 ingested, since v1's keys carry no tenant.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "CaptureChecksum",
    "RawStagingCatalog",
    "StagingCapture",
    "capture_checksum",
    "captures_to_stage",
    "checksums_to_clean",
]


@dataclass(frozen=True)
class StagingCapture:
    capture_id: uuid.UUID
    #: Share-relative NAS path (v1's convention); None for a frame held only
    #: in the object store, which staging counts as ``no_path``.
    source_path: str | None
    checksum: str


@dataclass(frozen=True)
class CaptureChecksum:
    checksum: str
    #: Migrated from v1: its processed JPEGs may still be where v1 wrote them.
    from_v1: bool


async def captures_to_stage(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[StagingCapture]:
    """The dive's canonical captures, with their NAS paths and checksums."""
    rows = await conn.execute(
        text("""
            SELECT id, source_path, checksum FROM captures
            WHERE tenant_id = :tenant AND dive_id = :dive AND is_canonical
            ORDER BY source_path NULLS LAST, id
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [StagingCapture(r.id, r.source_path, r.checksum) for r in rows]


async def checksums_to_clean(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[str]:
    """The checksums whose scratch this dive may delete: every one of its
    captures', canonical or not, except where the canonical copy is another
    dive's -- that dive staged it, and its cleanup deletes it."""
    rows = await conn.execute(
        text("""
            SELECT DISTINCT c.checksum FROM captures c
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive
              AND NOT EXISTS (
                  SELECT 1 FROM captures o
                  WHERE o.tenant_id = c.tenant_id
                    AND o.checksum_algorithm = c.checksum_algorithm
                    AND o.checksum = c.checksum
                    AND o.is_canonical
                    AND o.dive_id IS DISTINCT FROM c.dive_id
              )
            ORDER BY c.checksum
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return list(rows.scalars())


async def capture_checksum(
    conn: AsyncConnection, tenant_id: uuid.UUID, capture_id: uuid.UUID
) -> CaptureChecksum | None:
    """The capture's checksum, and whether it came from v1; None if the tenant
    has no such capture."""
    row = (
        await conn.execute(
            text("""
                SELECT checksum, v1_id IS NOT NULL AS from_v1 FROM captures
                WHERE tenant_id = :tenant AND id = :capture
                """),
            {"tenant": tenant_id, "capture": capture_id},
        )
    ).one_or_none()
    return None if row is None else CaptureChecksum(row.checksum, row.from_v1)


class RawStagingCatalog(ServicePrincipal):
    """Raw staging's database side, as the orchestrator's service principal."""

    async def captures_to_stage(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[StagingCapture]:
        async with self._tenant(tenant_id) as conn:
            return await captures_to_stage(conn, tenant_id, dive_id)

    async def checksums_to_clean(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[str]:
        async with self._tenant(tenant_id) as conn:
            return await checksums_to_clean(conn, tenant_id, dive_id)

    async def capture_checksum(
        self, tenant_id: uuid.UUID, capture_id: uuid.UUID
    ) -> CaptureChecksum | None:
        async with self._tenant(tenant_id) as conn:
            return await capture_checksum(conn, tenant_id, capture_id)
