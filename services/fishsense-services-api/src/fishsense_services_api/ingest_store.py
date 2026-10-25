"""The database side of dive ingest, run in a tenant transaction as the app role.

Ported from fishsense-lite@a8b2c3bc -- the writes behind
`create_mission_activity`, `finalize_mission_activity`, and the API's image upsert
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
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_facts import RULED
from fishsense_services_api.mission_store import PortIsRuled
from fishsense_services_api.service_principal import NotAMember, ServicePrincipal

__all__ = [
    "ContentOverlap",
    "IngestCatalog",
    "NotAMember",
    "RegisteredCapture",
    "ResolvedDevice",
    "content_overlap",
    "create_mission",
    "mission_by_path",
    "mission_port",
    "missions_with_leaf",
    "finalize_mission",
    "register_capture",
    "registered_captures",
    "resolve_device",
    "slate_template_by_name",
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
    mission_id: uuid.UUID
    mission_path: str
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


async def mission_port(
    conn: AsyncConnection, tenant_id: uuid.UUID, path: str
) -> tuple[str, str | None] | None:
    """(port, port_source) of the tenant's mission at ``path``, or None."""
    row = (
        await conn.execute(
            text(
                "SELECT port, port_source FROM missions "
                "WHERE tenant_id = :t AND source_path = :p"
            ),
            {"t": tenant_id, "p": path},
        )
    ).one_or_none()
    return None if row is None else (row.port, row.port_source)


#: On a re-ingest, the mission's port stands: nothing new was stated, or the
#: owner's rules said it.
_KEEP_PORT = "excluded.port = 'unknown' OR missions.port_source IN ({})".format(
    ", ".join(f"'{source}'" for source in RULED)
)


async def create_mission(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    source_path: str,
    name: str,
    started_at: datetime,
    device_id: uuid.UUID | None = None,
    slate_template_id: uuid.UUID | None = None,
    calibration_source_mission_id: uuid.UUID | None = None,
    flip_mission_slate: bool = False,
    port: str = "unknown",
    port_source: str | None = None,
    port_note: str | None = None,
    purpose: str | None = None,
) -> uuid.UUID:
    """Create (or re-open) the dive at **low**, whatever the request asked for.

    `started_at` is provisional -- preflight's max header timestamp -- and finalize
    replaces it with the scan's. Re-running ingest returns the same dive, back
    at low until finalize promotes it again.

    0047: re-opening never forgets what is known. An ``unknown`` port or an
    unstated purpose leaves the mission's as they are, a port by the owner's
    rules (``backfill``, ``device_suffix``) is kept, and a different one stated
    against it is refused (`PortIsRuled`) rather than silently dropped.
    """
    existing = (
        await conn.execute(
            text(
                "SELECT port, port_source FROM missions "
                "WHERE tenant_id = :t AND source_path = :p FOR UPDATE"
            ),
            {"t": tenant_id, "p": source_path},
        )
    ).one_or_none()
    if (
        existing is not None
        and existing.port_source in RULED
        and port not in ("unknown", existing.port)
    ):
        raise PortIsRuled(
            f"{source_path} is {existing.port} by the owner's rule "
            f"({existing.port_source}); ingest stated {port}"
        )
    return (
        await conn.execute(
            text("""
                INSERT INTO missions (tenant_id, source_path, name, started_at, priority,
                                   device_id, slate_template_id,
                                   calibration_source_mission_id, flip_mission_slate,
                                   port, port_source, port_note, purpose)
                VALUES (:tenant, :path, :name, :started_at, 'low', :device, :slate,
                        :source, :flip, :port, :port_source, :port_note, :purpose)
                ON CONFLICT (tenant_id, source_path) DO UPDATE SET
                    name = excluded.name,
                    started_at = excluded.started_at,
                    priority = 'low',
                    device_id = excluded.device_id,
                    slate_template_id = excluded.slate_template_id,
                    calibration_source_mission_id = excluded.calibration_source_mission_id,
                    flip_mission_slate = excluded.flip_mission_slate,
                    port = CASE WHEN {keep} THEN missions.port
                                ELSE excluded.port END,
                    port_source = CASE WHEN {keep} THEN missions.port_source
                                       ELSE excluded.port_source END,
                    port_note = CASE WHEN {keep} THEN missions.port_note
                                     ELSE excluded.port_note END,
                    purpose = coalesce(excluded.purpose, missions.purpose)
                RETURNING id
                """.format(keep=_KEEP_PORT)),
            {
                "tenant": tenant_id,
                "path": source_path,
                "name": name,
                "started_at": started_at,
                "device": device_id,
                "slate": slate_template_id,
                "source": calibration_source_mission_id,
                "flip": flip_mission_slate,
                "port": port,
                "port_source": port_source,
                "port_note": port_note,
                "purpose": purpose,
            },
        )
    ).scalar_one()


async def register_capture(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    mission_id: uuid.UUID,
    device_id: uuid.UUID | None,
    source_path: str,
    captured_at: datetime,
    checksum: str,
    focal_length_mm: float | None = None,
) -> RegisteredCapture:
    """Upsert one frame on its path; canonical unless another canonical copy of
    the same content exists in the tenant. `focal_length_mm` is EXIF 0x920A
    (0069, the zoom gate's input); None leaves what was recorded."""
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
        "mission": mission_id,
        "device": device_id,
        "path": source_path,
        "captured_at": captured_at,
        "checksum": checksum,
        "canonical": not canonical_elsewhere,
        "focal": focal_length_mm,
    }
    if existing is None:
        capture_id = (
            await conn.execute(
                text("""
                    INSERT INTO captures (tenant_id, mission_id, device_id, source_path,
                                          captured_at, checksum, is_canonical,
                                          focal_length_mm)
                    VALUES (:tenant, :mission, :device, :path, :captured_at, :checksum,
                            :canonical, :focal)
                    RETURNING id
                    """),
                params,
            )
        ).scalar_one()
    else:
        capture_id = existing
        await conn.execute(
            text("""
                UPDATE captures SET mission_id = :mission, device_id = :device,
                    captured_at = :captured_at, checksum = :checksum,
                    is_canonical = :canonical,
                    focal_length_mm = coalesce(:focal, focal_length_mm)
                WHERE tenant_id = :tenant AND id = :id
                """),
            {**params, "id": existing},
        )
    return RegisteredCapture(capture_id, not canonical_elsewhere)


async def registered_captures(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> dict[str, datetime]:
    """Path -> captured_at for frames already registered to the dive: what a
    resumed scan skips without downloading, and still counts towards the dive's
    max timestamp."""
    rows = await conn.execute(
        text(
            "SELECT source_path, captured_at FROM captures "
            "WHERE tenant_id = :tenant AND mission_id = :mission"
        ),
        {"tenant": tenant_id, "mission": mission_id},
    )
    return {r.source_path: r.captured_at for r in rows}


async def finalize_mission(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    priority: str,
    started_at: datetime,
) -> None:
    """Open the commit flag. Only call once every frame is accounted for -- the
    finalize activity enforces that before it gets here."""
    await conn.execute(
        text(
            "UPDATE missions SET priority = :priority, started_at = :started_at "
            "WHERE tenant_id = :tenant AND id = :mission"
        ),
        {
            "priority": priority,
            "started_at": started_at,
            "tenant": tenant_id,
            "mission": mission_id,
        },
    )


async def content_overlap(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> list[ContentOverlap]:
    """How much of this dive's content already exists under other dives in the
    tenant. Reported, never blocking."""
    rows = await conn.execute(
        text("""
            WITH mine AS (
                SELECT DISTINCT checksum FROM captures
                WHERE tenant_id = :tenant AND mission_id = :mission
            ), shared AS (
                SELECT c.mission_id, count(DISTINCT c.checksum) AS n
                FROM captures c JOIN mine USING (checksum)
                WHERE c.tenant_id = :tenant AND c.mission_id <> :mission
                GROUP BY c.mission_id
            )
            SELECT s.mission_id, d.source_path, s.n,
                   s.n::float / (SELECT count(*) FROM mine) AS containment
            FROM shared s JOIN missions d ON d.id = s.mission_id
            ORDER BY s.n DESC, d.source_path
            """),
        {"tenant": tenant_id, "mission": mission_id},
    )
    return [
        ContentOverlap(r.mission_id, r.source_path, r.n, r.containment) for r in rows
    ]


async def mission_by_path(
    conn: AsyncConnection, tenant_id: uuid.UUID, path: str
) -> uuid.UUID | None:
    """The tenant's dive at exactly this path."""
    return (
        await conn.execute(
            text(
                "SELECT id FROM missions WHERE tenant_id = :tenant AND source_path = :path"
            ),
            {"tenant": tenant_id, "path": path},
        )
    ).scalar_one_or_none()


async def missions_with_leaf(
    conn: AsyncConnection, tenant_id: uuid.UUID, leaf: str
) -> list[tuple[uuid.UUID, str]]:
    """The tenant's dives whose folder name is exactly ``leaf``.

    Compared as a suffix, not with LIKE: dive names are full of ``_``, which
    LIKE would treat as a wildcard.
    """
    rows = await conn.execute(
        text("""
            SELECT id, source_path FROM missions
            WHERE tenant_id = :tenant
              AND (source_path = :leaf
                   OR right(source_path, length(:leaf) + 1) = '/' || :leaf)
            ORDER BY source_path
            """),
        {"tenant": tenant_id, "leaf": leaf},
    )
    return [(r.id, r.source_path) for r in rows]


async def slate_template_by_name(conn: AsyncConnection, name: str) -> uuid.UUID | None:
    """The (global) slate template with exactly this name."""
    return (
        await conn.execute(
            text("SELECT id FROM slate_templates WHERE name = :name"), {"name": name}
        )
    ).scalar_one_or_none()


class IngestCatalog(ServicePrincipal):
    """Ingest's database side, as the orchestrator's service principal (see
    `ServicePrincipal`: every call re-checks the membership). Satisfies the
    orchestrator's ``Catalog`` protocol."""

    async def resolve_device(
        self, tenant_id: uuid.UUID, serial: str
    ) -> ResolvedDevice | None:
        async with self._tenant(tenant_id) as conn:
            return await resolve_device(conn, tenant_id, serial)

    async def mission_by_path(
        self, tenant_id: uuid.UUID, path: str
    ) -> uuid.UUID | None:
        async with self._tenant(tenant_id) as conn:
            return await mission_by_path(conn, tenant_id, path)

    async def missions_with_leaf(
        self, tenant_id: uuid.UUID, leaf: str
    ) -> list[tuple[uuid.UUID, str]]:
        async with self._tenant(tenant_id) as conn:
            return await missions_with_leaf(conn, tenant_id, leaf)

    async def mission_port(
        self, tenant_id: uuid.UUID, path: str
    ) -> tuple[str, str | None] | None:
        async with self._tenant(tenant_id) as conn:
            return await mission_port(conn, tenant_id, path)

    async def slate_template(self, name: str) -> uuid.UUID | None:
        async with self._engine.connect() as conn:
            return await slate_template_by_name(conn, name)

    async def create_mission(self, tenant_id: uuid.UUID, **mission: Any) -> uuid.UUID:
        async with self._tenant(tenant_id) as conn:
            return await create_mission(conn, tenant_id, **mission)

    async def register_capture(
        self, tenant_id: uuid.UUID, **capture: Any
    ) -> RegisteredCapture:
        async with self._tenant(tenant_id) as conn:
            return await register_capture(conn, tenant_id, **capture)

    async def registered_captures(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> dict[str, datetime]:
        async with self._tenant(tenant_id) as conn:
            return await registered_captures(conn, tenant_id, mission_id)

    async def finalize_mission(
        self,
        tenant_id: uuid.UUID,
        mission_id: uuid.UUID,
        *,
        priority: str,
        started_at: datetime,
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await finalize_mission(
                conn, tenant_id, mission_id, priority=priority, started_at=started_at
            )

    async def content_overlap(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> list[ContentOverlap]:
        async with self._tenant(tenant_id) as conn:
            return await content_overlap(conn, tenant_id, mission_id)
