"""The database side of dive ingest, run in a tenant transaction as the app role.

Ported from fishsense-lite@a8b2c3bc -- the writes behind
`create_dive_activity`, `finalize_dive_activity`, and the API's image upsert
(`image_controller.post_image`) -- as plain functions over a tenant-scoped
connection, so RLS applies to every statement. The orchestrator's activities
call them; there is no HTTP hop inside the control plane (PLAN.md §6.3).

v1's semantics, kept:

* a dive is created at **low** whatever was asked, upserting on its path --
  priority is the commit flag, and every cohort ignores low;
* a capture upserts on its path; the dive's datetime is the scanned max;
* containment is `|shared| / |this dive's checksums|`, excluding itself.

v2 changes:

* everything is per tenant, including which copy of a frame is canonical;
* **canonical unless another canonical copy exists.** v1 checked for *any*
  other row with the checksum, so re-posting the canonical copy after a
  duplicate landed would demote it and leave the frame with no canonical copy.
  The partial unique index still guarantees at most one; a registration that
  loses a race fails and its retry re-reads the winner, as in v1.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "ContentOverlap",
    "RegisteredCapture",
    "ResolvedDevice",
    "content_overlap",
    "create_dive",
    "finalize_dive",
    "register_capture",
    "registered_paths",
    "resolve_device",
]


@dataclass(frozen=True)
class ResolvedDevice:
    device_id: uuid.UUID
    name: str | None
    #: A camera calibration exists -- without one, stage 14 can never measure.
    has_camera_calibration: bool


@dataclass(frozen=True)
class RegisteredCapture:
    capture_id: uuid.UUID
    is_canonical: bool


@dataclass(frozen=True)
class ContentOverlap:
    dive_id: uuid.UUID
    dive_path: str
    shared_images: int
    containment: float


async def resolve_device(
    conn: AsyncConnection, tenant_id: uuid.UUID, serial: str
) -> ResolvedDevice | None:
    """The tenant's device with this serial -- never another tenant's."""
    row = (
        await conn.execute(
            text("""
                SELECT d.id, d.name, EXISTS (
                    SELECT 1 FROM camera_calibrations c
                    WHERE c.tenant_id = d.tenant_id AND c.device_id = d.id
                ) AS calibrated
                FROM devices d
                WHERE d.tenant_id = :tenant AND d.serial = :serial
                """),
            {"tenant": tenant_id, "serial": serial},
        )
    ).one_or_none()
    if row is None:
        return None
    return ResolvedDevice(row.id, row.name, row.calibrated)


async def create_dive(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    source_path: str,
    name: str,
    dived_at: datetime,
    device_id: uuid.UUID | None = None,
    slate_template_id: uuid.UUID | None = None,
    calibration_source_dive_id: uuid.UUID | None = None,
    flip_dive_slate: bool = False,
) -> uuid.UUID:
    """Create (or re-open) the dive at **low**, whatever the request asked for.

    `dived_at` is provisional -- preflight's max header timestamp -- and finalize
    replaces it with the scan's. Re-running ingest returns the same dive, back
    at low until finalize promotes it again.
    """
    return (
        await conn.execute(
            text("""
                INSERT INTO dives (tenant_id, source_path, name, dived_at, priority,
                                   device_id, slate_template_id,
                                   calibration_source_dive_id, flip_dive_slate)
                VALUES (:tenant, :path, :name, :dived_at, 'low', :device, :slate,
                        :source, :flip)
                ON CONFLICT (tenant_id, source_path) DO UPDATE SET
                    name = excluded.name,
                    dived_at = excluded.dived_at,
                    priority = 'low',
                    device_id = excluded.device_id,
                    slate_template_id = excluded.slate_template_id,
                    calibration_source_dive_id = excluded.calibration_source_dive_id,
                    flip_dive_slate = excluded.flip_dive_slate
                RETURNING id
                """),
            {
                "tenant": tenant_id,
                "path": source_path,
                "name": name,
                "dived_at": dived_at,
                "device": device_id,
                "slate": slate_template_id,
                "source": calibration_source_dive_id,
                "flip": flip_dive_slate,
            },
        )
    ).scalar_one()


async def register_capture(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    dive_id: uuid.UUID,
    device_id: uuid.UUID | None,
    source_path: str,
    captured_at: datetime,
    checksum: str,
) -> RegisteredCapture:
    """Upsert one frame on its path; canonical unless another canonical copy of
    the same content exists in the tenant."""
    existing = (
        await conn.execute(
            text(
                "SELECT id FROM captures WHERE tenant_id = :tenant AND source_path = :path"
            ),
            {"tenant": tenant_id, "path": source_path},
        )
    ).scalar_one_or_none()
    canonical_elsewhere = (
        await conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM captures
                    WHERE tenant_id = :tenant AND checksum_algorithm = 'md5'
                      AND checksum = :checksum AND is_canonical
                      AND id IS DISTINCT FROM :self
                )
                """),
            {"tenant": tenant_id, "checksum": checksum, "self": existing},
        )
    ).scalar_one()
    params = {
        "tenant": tenant_id,
        "dive": dive_id,
        "device": device_id,
        "path": source_path,
        "captured_at": captured_at,
        "checksum": checksum,
        "canonical": not canonical_elsewhere,
    }
    if existing is None:
        capture_id = (
            await conn.execute(
                text("""
                    INSERT INTO captures (tenant_id, dive_id, device_id, source_path,
                                          captured_at, checksum, is_canonical)
                    VALUES (:tenant, :dive, :device, :path, :captured_at, :checksum,
                            :canonical)
                    RETURNING id
                    """),
                params,
            )
        ).scalar_one()
    else:
        capture_id = existing
        await conn.execute(
            text("""
                UPDATE captures SET dive_id = :dive, device_id = :device,
                    captured_at = :captured_at, checksum = :checksum,
                    is_canonical = :canonical
                WHERE tenant_id = :tenant AND id = :id
                """),
            {**params, "id": existing},
        )
    return RegisteredCapture(capture_id, not canonical_elsewhere)


async def registered_paths(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> set[str]:
    """Paths already registered for the dive: what a resumed scan skips without
    downloading."""
    rows = await conn.execute(
        text(
            "SELECT source_path FROM captures "
            "WHERE tenant_id = :tenant AND dive_id = :dive"
        ),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return set(rows.scalars())


async def finalize_dive(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    priority: str,
    dived_at: datetime,
) -> None:
    """Open the commit flag. Only call once every frame is accounted for -- the
    finalize activity enforces that before it gets here."""
    await conn.execute(
        text(
            "UPDATE dives SET priority = :priority, dived_at = :dived_at "
            "WHERE tenant_id = :tenant AND id = :dive"
        ),
        {
            "priority": priority,
            "dived_at": dived_at,
            "tenant": tenant_id,
            "dive": dive_id,
        },
    )


async def content_overlap(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[ContentOverlap]:
    """How much of this dive's content already exists under other dives in the
    tenant. Reported, never blocking."""
    rows = await conn.execute(
        text("""
            WITH mine AS (
                SELECT DISTINCT checksum FROM captures
                WHERE tenant_id = :tenant AND dive_id = :dive
            ), shared AS (
                SELECT c.dive_id, count(DISTINCT c.checksum) AS n
                FROM captures c JOIN mine USING (checksum)
                WHERE c.tenant_id = :tenant AND c.dive_id <> :dive
                GROUP BY c.dive_id
            )
            SELECT s.dive_id, d.source_path, s.n,
                   s.n::float / (SELECT count(*) FROM mine) AS containment
            FROM shared s JOIN dives d ON d.id = s.dive_id
            ORDER BY s.n DESC, d.source_path
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [ContentOverlap(r.dive_id, r.source_path, r.n, r.containment) for r in rows]
