"""The laser-color backfill's database side (0076; the owner, 2026-10-26).

New in v2. Automatic dots predicted before contract v22 kept no color, and
head/tail v5 never runs the dot step again. The backfill reads each dot's
color off its prediction's render (contract v25, the processor's
`laser_colour_backfill`). Here:

* the work: every canonical capture's current automatic prediction with a dot
  and a render, a mission at a time, a page at a time -- dots that already
  have a color included, as the control: the live kernel read those off the
  raw, and a render reading that disagrees with it is the change of input
  showing;
* a page's readings counted, and compared with the labelers' color (live,
  completed labels naming one) and the live kernel's on the same frames;
* with ``apply``, a reading written to ``automatic_laser_colour_readings``
  only for a dot that is still its frame's current prediction, has no color
  of its own, and has no reading at this version. Only ``read`` frames are
  written (a color, or too close to call); a render that wasn't there stays
  owed.

The source and version are the contract's (`laser_colour_backfill.
COLOUR_READING_SOURCE`/`_VERSION`), repeated because the API image has no
contracts; a test pins them equal.

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "ColourBackfillCatalog",
    "ColourBackfillMission",
    "ColourReadingRow",
    "ColourWork",
    "READING_SOURCE",
    "READING_VERSION",
    "colour_backfill_frames",
    "colour_backfill_missions",
    "record_colour_page",
]

READING_SOURCE = "render"
READING_VERSION = 1

_WORK = """
    SELECT p.id AS prediction_id, p.seq, p.capture_id, c.mission_id,
           p.laser_x, p.laser_y, p.laser_color AS kernel_color,
           p.jpeg_render_version AS render_version,
           p.jpeg_camera_calibration_id AS camera_calibration_id
    FROM current_automatic_head_tail_predictions p
    JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
    WHERE p.tenant_id = :t AND c.is_canonical
      AND p.laser_x IS NOT NULL AND p.laser_y IS NOT NULL
      AND p.jpeg_render_version IS NOT NULL
"""


@dataclass(frozen=True)
class ColourBackfillMission:
    mission_id: uuid.UUID
    number: int
    frames: int


@dataclass(frozen=True)
class ColourWork:
    prediction_id: uuid.UUID
    seq: int
    capture_id: uuid.UUID
    x: float
    y: float
    #: The dot's own color (0072), as the live kernel read it; None before.
    kernel_color: str | None
    render_version: int
    camera_calibration_id: uuid.UUID | None


@dataclass(frozen=True)
class ColourReadingRow:
    """A processor reading (contract `ColourReading`), as the store takes it."""

    prediction_id: uuid.UUID
    capture_id: uuid.UUID
    status: str
    color: str | None = None
    margin: float | None = None


async def colour_backfill_missions(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[ColourBackfillMission]:
    """Each mission with a dot to read, and how many, by number."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.number, count(*) AS n
            FROM ({_WORK}) w JOIN missions d ON d.tenant_id = :t AND d.id = w.mission_id
            GROUP BY d.id, d.number ORDER BY d.number
            """),
        {"t": tenant_id},
    )
    return [ColourBackfillMission(r.id, r.number, r.n) for r in rows]


async def colour_backfill_frames(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> list[ColourWork]:
    """A page of one mission's dots, in `seq` order."""
    rows = await conn.execute(
        text(f"""
            SELECT * FROM ({_WORK}) w WHERE w.mission_id = :d AND w.seq > :after
            ORDER BY w.seq LIMIT :limit
            """),
        {"t": tenant_id, "d": mission_id, "after": after_seq, "limit": limit},
    )
    return [
        ColourWork(prediction_id=r.prediction_id, seq=r.seq, capture_id=r.capture_id,
                   x=r.laser_x, y=r.laser_y, kernel_color=r.kernel_color,
                   render_version=r.render_version,
                   camera_calibration_id=r.camera_calibration_id)  # fmt: skip
        for r in rows
    ]


def _read_as(r: ColourReadingRow) -> str:
    if r.color is not None:
        return r.color
    return "too_close" if r.margin is not None else "unreadable"


async def record_colour_page(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    readings: list[ColourReadingRow],
    *,
    apply: bool,
) -> dict:
    """Count a page's readings and compare them; with ``apply``, write the
    ones still owed. Returns plain counts (`dict`s keyed by color, and
    ``"<reading>/<label or kernel>"`` for the comparisons)."""
    if not readings:
        return {"status": {}, "read": {}, "vs_label": {}, "vs_kernel": {}, "written": 0}
    ids = [r.capture_id for r in readings]
    owned = set(
        (
            await conn.execute(
                text("SELECT id FROM captures WHERE tenant_id = :t AND mission_id = :m "
                     "AND id = ANY(CAST(:ids AS uuid[]))"),
                {"t": tenant_id, "m": mission_id, "ids": ids},
            )
        ).scalars()
    )  # fmt: skip
    if stray := [r.capture_id for r in readings if r.capture_id not in owned]:
        raise ValueError(f"captures not the mission's: {stray[:3]}")

    labels: dict[uuid.UUID, set[str]] = {}
    for row in await conn.execute(
        text("""
            SELECT capture_id, CASE label WHEN 'Red Laser' THEN 'red' ELSE 'green' END AS c
            FROM laser_labels
            WHERE tenant_id = :t AND capture_id = ANY(CAST(:ids AS uuid[]))
              AND completed AND NOT superseded AND label IN ('Red Laser', 'Green Laser')
            """),
        {"t": tenant_id, "ids": ids},
    ):
        labels.setdefault(row.capture_id, set()).add(row.c)
    current = {
        row.id: row.laser_color
        for row in await conn.execute(
            text("""
                SELECT id, laser_color FROM current_automatic_head_tail_predictions
                WHERE tenant_id = :t AND capture_id = ANY(CAST(:ids AS uuid[]))
                """),
            {"t": tenant_id, "ids": ids},
        )
    }

    status, read, vs_label, vs_kernel = Counter(), Counter(), Counter(), Counter()
    for r in readings:
        if r.status != "read":
            status[r.status] += 1
            continue
        read[_read_as(r)] += 1
        if r.color is None:
            continue
        label = labels.get(r.capture_id, set())
        if len(label) == 1:
            vs_label[f"{r.color}/{next(iter(label))}"] += 1
        elif label:
            vs_label[f"{r.color}/mixed"] += 1
        if kernel := current.get(r.prediction_id):
            vs_kernel[f"{r.color}/{kernel}"] += 1

    written = 0
    if apply:
        for r in readings:
            if r.status != "read" or r.prediction_id not in current:
                continue
            if current[r.prediction_id] is not None:
                continue
            written += (
                await conn.execute(
                    text("""
                        INSERT INTO automatic_laser_colour_readings
                            (tenant_id, capture_id, automatic_head_tail_prediction_id,
                             color, color_margin, source, reading_version)
                        SELECT :t, :c, :p, :color, :margin, :source, :version
                        WHERE NOT EXISTS (
                            SELECT 1 FROM automatic_laser_colour_readings x
                            WHERE x.tenant_id = :t
                              AND x.automatic_head_tail_prediction_id = :p
                              AND x.reading_version = :version)
                        """),
                    {"t": tenant_id, "c": r.capture_id, "p": r.prediction_id,
                     "color": r.color, "margin": r.margin, "source": READING_SOURCE,
                     "version": READING_VERSION},
                )  # fmt: skip
            ).rowcount
    return {"status": dict(status), "read": dict(read), "vs_label": dict(vs_label),
            "vs_kernel": dict(vs_kernel), "written": written}  # fmt: skip


class ColourBackfillCatalog(ServicePrincipal):
    """The backfill's database side, as the orchestrator's service principal."""

    async def colour_backfill_missions(self, tenant_id):
        async with self._tenant(tenant_id) as conn:
            return await colour_backfill_missions(conn, tenant_id)

    async def colour_backfill_frames(self, tenant_id, mission_id, *, after_seq=0,
                                     limit=200):  # fmt: skip
        async with self._tenant(tenant_id) as conn:
            return await colour_backfill_frames(
                conn, tenant_id, mission_id, after_seq=after_seq, limit=limit
            )

    async def record_colour_page(self, tenant_id, mission_id, readings, *, apply):
        async with self._tenant(tenant_id) as conn:
            return await record_colour_page(
                conn, tenant_id, mission_id, readings, apply=apply
            )
