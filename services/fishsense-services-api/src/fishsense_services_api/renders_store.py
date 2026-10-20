"""Every capture's render (0050): what is owed, and what was rendered where.

New in v2 (2026-10-19). A capture is owed a render at a render version until
it has a row -- a render or an abstention -- at that version under its
device's current pinhole camera calibration: a corrected camera owes every
capture a new one, since the pixels change. Missions with a human
measurement come first (their fish are on /fish with no photo until then),
then the oldest. A mission whose device has no pinhole calibration cannot be
rectified, and owes nothing here.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.automatic_results_store import (
    _check_captures,
    _flat,
    _pinhole_camera,
)
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "CurrentRender",
    "RenderCapture",
    "RenderInputs",
    "RenderMission",
    "RenderRow",
    "RendersCatalog",
    "current_renders",
    "persist_renders",
    "render_inputs",
    "render_missions",
]

#: A capture of mission `d` with no row at render `:rv` under camera `cc`.
_OWED = """
    c.tenant_id = d.tenant_id AND c.mission_id = d.id AND c.is_canonical
    AND NOT EXISTS (
        SELECT 1 FROM capture_renders r
        WHERE r.tenant_id = c.tenant_id AND r.capture_id = c.id
          AND r.render_version = :rv AND r.camera_calibration_id = cc.id
    )"""

#: Mission `d`'s device's current pinhole calibration, `cc`.
_CAMERA = """
    JOIN current_camera_calibrations cc
      ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
     AND cc.camera_model = 'pinhole'"""


@dataclass(frozen=True)
class RenderMission:
    mission_id: uuid.UUID
    #: Captures owed a render.
    owed: int
    #: A human measurement exists on one of its captures.
    human_measured: bool


@dataclass(frozen=True)
class RenderCapture:
    capture_id: uuid.UUID
    checksum: str


@dataclass(frozen=True)
class RenderInputs:
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    captures: list[RenderCapture]


@dataclass(frozen=True)
class RenderRow:
    """One capture's render, or why there is none, to append."""

    capture_id: uuid.UUID
    status: str
    render_version: int
    camera_calibration_id: uuid.UUID
    bucket: str | None = None
    key: str | None = None
    bytes: int | None = None
    md5: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class CurrentRender:
    capture_id: uuid.UUID
    bucket: str
    key: str
    render_version: int
    camera_calibration_id: uuid.UUID


async def render_missions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    render_version: int,
    limit: int | None = None,
) -> list[RenderMission]:
    """The tenant's missions owing renders: human-measured first, then oldest."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, owed.n,
                   EXISTS (SELECT 1 FROM measurements m
                           JOIN captures mc ON mc.tenant_id = m.tenant_id
                                           AND mc.id = m.capture_id
                           WHERE mc.tenant_id = d.tenant_id
                             AND mc.mission_id = d.id) AS human_measured
            FROM missions d
            {_CAMERA}
            CROSS JOIN LATERAL (
                SELECT count(*) AS n FROM captures c WHERE {_OWED}
            ) owed
            WHERE d.tenant_id = :t AND owed.n > 0
            ORDER BY human_measured DESC, d.created_at, d.id
            LIMIT :limit
            """),
        {"t": tenant_id, "rv": render_version, "limit": limit},
    )
    return [RenderMission(r.id, r.n, r.human_measured) for r in rows]


async def render_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    render_version: int,
) -> RenderInputs:
    """The mission's camera, and its captures owed a render, in capture order."""
    camera = await _pinhole_camera(conn, tenant_id, mission_id)
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum
            FROM missions d
            {_CAMERA}
            JOIN captures c ON {_OWED}
            WHERE d.tenant_id = :t AND d.id = :d
            ORDER BY c.number, c.id
            """),
        {"t": tenant_id, "d": mission_id, "rv": render_version},
    )
    return RenderInputs(
        camera_calibration_id=camera.id,
        camera_matrix=[[float(x) for x in r] for r in camera.camera_matrix],
        distortion_coefficients=_flat(camera.distortion_coefficients),
        captures=[RenderCapture(r.id, r.checksum) for r in rows],
    )


async def persist_renders(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    rows: list[RenderRow],
) -> int:
    """Append the mission's renders and abstentions, all or nothing; a render
    already recorded at its version and camera is not recorded twice.
    Returns how many rows were written."""
    rows = list(rows)
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, mission_id, [r.capture_id for r in rows])
    written = 0
    for r in rows:
        values = asdict(r)
        columns = ", ".join(["tenant_id", *values])
        params = ", ".join([":tenant_id", *(f":{k}" for k in values)])
        result = await conn.execute(
            text(f"""
                INSERT INTO capture_renders ({columns}) VALUES ({params})
                ON CONFLICT (tenant_id, capture_id, render_version,
                             camera_calibration_id)
                    WHERE status = 'rendered' DO NOTHING
                """),
            {"tenant_id": tenant_id, **values},
        )
        written += result.rowcount
    return written


async def current_renders(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_ids: list[uuid.UUID],
) -> dict[uuid.UUID, CurrentRender]:
    """Each capture's newest render, where it has one."""
    rows = await conn.execute(
        text("""
            SELECT DISTINCT ON (r.capture_id) r.capture_id, r.bucket, r.key,
                   r.render_version, r.camera_calibration_id
            FROM capture_renders r
            WHERE r.tenant_id = :t AND r.capture_id = ANY(:ids)
              AND r.status = 'rendered'
            ORDER BY r.capture_id, r.seq DESC
            """),
        {"t": tenant_id, "ids": list(capture_ids)},
    )
    return {
        r.capture_id: CurrentRender(r.capture_id, r.bucket, r.key, r.render_version,
                                    r.camera_calibration_id)  # fmt: skip
        for r in rows
    }


class RendersCatalog(ServicePrincipal):
    """The render stage's database side, as the orchestrator's service
    principal: every call rechecks its membership of the tenant."""

    async def tenant_slug(self, tenant_id: uuid.UUID) -> str:
        """The tenant's slug, which names its bucket in the tenant stores."""
        async with self._tenant(tenant_id) as conn:
            return (
                await conn.execute(
                    text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id}
                )
            ).scalar_one()

    async def render_missions(
        self, tenant_id: uuid.UUID, *, render_version: int, limit: int | None = None
    ) -> list[RenderMission]:
        async with self._tenant(tenant_id) as conn:
            return await render_missions(
                conn, tenant_id, render_version=render_version, limit=limit
            )

    async def render_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, render_version: int
    ) -> RenderInputs:
        async with self._tenant(tenant_id) as conn:
            return await render_inputs(
                conn, tenant_id, mission_id, render_version=render_version
            )

    async def persist_renders(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, rows: list[RenderRow]
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_renders(conn, tenant_id, mission_id, rows)

    async def current_renders(
        self, tenant_id: uuid.UUID, capture_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, CurrentRender]:
        async with self._tenant(tenant_id) as conn:
            return await current_renders(conn, tenant_id, capture_ids)
