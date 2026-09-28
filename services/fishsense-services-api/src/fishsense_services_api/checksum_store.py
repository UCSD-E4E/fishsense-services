"""The database side of checksum verification, tenant-scoped and read-only.

Ported from fishsense-lite@77e8f8e5: v1's verifier read every image row of the
dive through the SDK (`fs.images.get(dive_id=...)` in
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/verify_dive_checksums_activity.py), and the sweep's selector read
`GET /api/v1/canonical/dives/` (services/fishsense-api/src/fishsense_api/
controllers/dive_controller.py:46; select_canonical_dive_ids_activity.py).

v1's rules, kept:

* **not canonical-filtered.** Half of v1's image rows are duplicates, and they
  are exactly the population whose provenance is least certain;
* the sweep visits every dive with **at least one canonical** capture, in id
  order;
* a dive's rows come back in path order (v1: ``sorted(key=path or "")``), so a
  sample of the first N is the same N on every run.

v2 changes: per tenant, as the orchestrator's service principal; a dive and a
capture are named by their ``number``, which is v1's id for a migrated row.

Nothing here writes: the verifier runs against production data to answer a
question and must not be able to change the answer.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "ChecksumCatalog",
    "VerifyCapture",
    "canonical_dive_numbers",
    "captures_to_verify",
    "dive_by_number",
]


@dataclass(frozen=True)
class VerifyCapture:
    """One frame as the verifier compares it with the file on the NAS."""

    #: v1's image id, for a migrated capture.
    number: int
    #: Share-relative NAS path (v1's convention); None for a frame held only
    #: in the object store, which the verifier cannot check.
    source_path: str | None
    checksum: str
    checksum_algorithm: str
    captured_at: datetime


async def dive_by_number(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> uuid.UUID | None:
    """The tenant's dive numbered ``number``."""
    return (
        await conn.execute(
            text("SELECT id FROM dives WHERE tenant_id = :tenant AND number = :n"),
            {"tenant": tenant_id, "n": number},
        )
    ).scalar_one_or_none()


async def captures_to_verify(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[VerifyCapture]:
    """Every capture of the dive, duplicates included, in path order."""
    rows = await conn.execute(
        text("""
            SELECT number, source_path, checksum, checksum_algorithm, captured_at
            FROM captures
            WHERE tenant_id = :tenant AND dive_id = :dive
            ORDER BY coalesce(source_path, ''), number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [
        VerifyCapture(
            r.number, r.source_path, r.checksum, r.checksum_algorithm, r.captured_at
        )
        for r in rows
    ]


async def canonical_dive_numbers(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[int]:
    """The tenant's dives with at least one canonical capture, by number."""
    rows = await conn.execute(
        text("""
            SELECT d.number FROM dives d
            WHERE d.tenant_id = :tenant
              AND EXISTS (
                  SELECT 1 FROM captures c
                  WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                    AND c.is_canonical
              )
            ORDER BY d.number
            """),
        {"tenant": tenant_id},
    )
    return list(rows.scalars())


class ChecksumCatalog(ServicePrincipal):
    """Checksum verification's database side, as the orchestrator's principal."""

    async def dive_by_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None:
        async with self._tenant(tenant_id) as conn:
            return await dive_by_number(conn, tenant_id, number)

    async def captures_to_verify(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[VerifyCapture]:
        async with self._tenant(tenant_id) as conn:
            return await captures_to_verify(conn, tenant_id, dive_id)

    async def canonical_dive_numbers(self, tenant_id: uuid.UUID) -> list[int]:
        async with self._tenant(tenant_id) as conn:
            return await canonical_dive_numbers(conn, tenant_id)
