"""A mission's laser colours, counted by source (the owner, 2026-10-24).

New in v2. One rig shoots one colour for a whole dive, and three things say
which, frame by frame:

* **labels**: the colour a labeller chose for the dot (``laser_labels.label``,
  "Red Laser" / "Green Laser"); live labels with a dot only;
* **predictions**: the human path's laser stage, read off the dot's pixels
  (``current_laser_predictions.color``; 98% per frame on 332 dots);
* **automatic**: the automatic chain's dot, read by the same kernel (0072),
  or off its render by the backfill (0076) when the dot kept none.

Counts, never a verdict: a mission whose sources disagree is worth seeing, and
a majority would hide it (the orchestrator's `mission_laser_label` takes one,
for Label Studio's pre-annotation, and keeps none). ``unread`` is a dot with
no colour: a label without one, or a prediction that abstained.

The current views narrow by capture id, not by mission (`automatic_lengths_
store` measured why), so they are handed the mission's capture ids.

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "ColourCounts",
    "MissionLaserColours",
    "dot_colour_columns",
    "dot_colour_join",
    "mission_laser_colours",
    "tenant_laser_colors",
]

#: How a label names each colour.
LABEL_COLOURS = {"Red Laser": "red", "Green Laser": "green"}


def dot_colour_join(h: str) -> str:
    """A LEFT JOIN of the automatic prediction ``h``'s latest backfilled
    reading (0076) as ``rc``: one index lookup per prediction."""
    return f"""LEFT JOIN LATERAL (
        SELECT r.seq, r.color, r.color_margin
        FROM automatic_laser_colour_readings r
        WHERE r.tenant_id = {h}.tenant_id
          AND r.automatic_head_tail_prediction_id = {h}.id
        ORDER BY r.seq DESC LIMIT 1
    ) rc ON true"""


def dot_colour_columns(h: str) -> str:
    """``laser_color``, ``laser_color_margin`` and ``laser_color_source`` of
    the automatic prediction ``h`` (with `dot_colour_join`'s ``rc``): the
    dot's own colour (0072), else its backfilled reading (``render``), else
    what the dot read when too close to call."""
    source = f"""CASE WHEN {h}.laser_color IS NOT NULL THEN 'dot'
                     WHEN rc.seq IS NOT NULL THEN 'render'
                     WHEN {h}.laser_color_margin IS NOT NULL THEN 'dot' END"""
    return f"""CASE WHEN {h}.laser_color IS NULL AND rc.seq IS NOT NULL
                    THEN rc.color ELSE {h}.laser_color END AS laser_color,
               CASE WHEN {h}.laser_color IS NULL AND rc.seq IS NOT NULL
                    THEN rc.color_margin ELSE {h}.laser_color_margin END
                   AS laser_color_margin,
               {source} AS laser_color_source"""


_COUNTS_SQL = f"""
    SELECT 'labels' AS source,
           CASE l.label WHEN 'Red Laser' THEN 'red' WHEN 'Green Laser' THEN 'green'
           END AS colour,
           count(DISTINCT l.capture_id) AS n
    FROM laser_labels l
    WHERE l.tenant_id = :t AND l.capture_id = ANY(CAST(:ids AS uuid[]))
      AND l.completed AND NOT l.superseded AND l.x IS NOT NULL
    GROUP BY 2
    UNION ALL
    SELECT 'predictions', p.color, count(*)
    FROM current_laser_predictions p
    WHERE p.tenant_id = :t AND p.capture_id = ANY(CAST(:ids AS uuid[]))
      AND p.x IS NOT NULL
    GROUP BY 2
    UNION ALL
    SELECT 'automatic', coalesce(h.laser_color, rc.color), count(*)
    FROM current_automatic_head_tail_predictions h
    {dot_colour_join("h")}
    WHERE h.tenant_id = :t AND h.capture_id = ANY(CAST(:ids AS uuid[]))
      AND h.laser_x IS NOT NULL
    GROUP BY 2
"""


@dataclass(frozen=True)
class ColourCounts:
    red: int = 0
    green: int = 0
    #: A dot with no colour: a label without one, or an abstaining reading.
    unread: int = 0


@dataclass(frozen=True)
class MissionLaserColours:
    labels: ColourCounts
    predictions: ColourCounts
    automatic: ColourCounts


async def mission_laser_colours(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> MissionLaserColours:
    counts: dict[str, dict[str, int]] = {
        "labels": {},
        "predictions": {},
        "automatic": {},
    }
    ids = (
        (
            await conn.execute(
                text(
                    "SELECT id FROM captures WHERE tenant_id = :t AND mission_id = :m"
                ),
                {"t": tenant_id, "m": mission_id},
            )
        )
        .scalars()
        .all()
    )
    for row in await conn.execute(text(_COUNTS_SQL), {"t": tenant_id, "ids": ids}):
        key = row.colour if row.colour in ("red", "green") else "unread"
        counts[row.source][key] = counts[row.source].get(key, 0) + row.n
    return MissionLaserColours(
        **{source: ColourCounts(**found) for source, found in counts.items()}
    )


#: Every mission's counts at once: the same three sources, grouped by mission,
#: each current view read once for the tenant (1.6 s for the lab in prod,
#: 2026-10-26, against ~60 ms a mission one at a time).
_TENANT_SQL = f"""
    SELECT 'labels' AS source, d.number,
           CASE l.label WHEN 'Red Laser' THEN 'red' WHEN 'Green Laser' THEN 'green'
           END AS color,
           count(DISTINCT l.capture_id) AS n
    FROM laser_labels l
    JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE l.tenant_id = :t AND l.completed AND NOT l.superseded AND l.x IS NOT NULL
    GROUP BY 2, 3
    UNION ALL
    SELECT 'predictions', d.number, p.color, count(*)
    FROM current_laser_predictions p
    JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE p.tenant_id = :t AND p.x IS NOT NULL
    GROUP BY 2, 3
    UNION ALL
    SELECT 'automatic', d.number, coalesce(h.laser_color, rc.color), count(*)
    FROM current_automatic_head_tail_predictions h
    {dot_colour_join("h")}
    JOIN captures c ON c.tenant_id = h.tenant_id AND c.id = h.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE h.tenant_id = :t AND h.laser_x IS NOT NULL
    GROUP BY 2, 3
"""


async def tenant_laser_colors(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> dict[int, MissionLaserColours]:
    """Every mission with a dot from any source, by number: what
    `mission_laser_colours` gives each, in one read (the owner, 2026-10-26)."""
    found: dict[int, dict[str, dict[str, int]]] = {}
    for row in await conn.execute(text(_TENANT_SQL), {"t": tenant_id}):
        counts = found.setdefault(
            row.number, {"labels": {}, "predictions": {}, "automatic": {}}
        )
        key = row.color if row.color in ("red", "green") else "unread"
        counts[row.source][key] = counts[row.source].get(key, 0) + row.n
    return {
        number: MissionLaserColours(
            **{source: ColourCounts(**c) for source, c in counts.items()}
        )
        for number, counts in sorted(found.items())
    }
