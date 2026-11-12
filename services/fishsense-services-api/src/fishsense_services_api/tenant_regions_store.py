"""Where a tenant's fish were measured, by broad region (`regions`), for its
Today map. Tenant-scoped, read in a tenant transaction like every store.

Coarse on purpose: the map is a picture of where the work happens (the
owner, 2026-10-21), so nothing finer than a region leaves here. Wild fish
only: fish models, the decoys of pool trials, are left off (the owner, the
same day), as on the public map. On production that took the lab's map from
3,314 fish to its 203 wild ones, and Southern California off it: Nathan's
Pool held fish models alone. A fish is placed by its mission's folder (a
pool at its region, for a wild fish measured in one); else by its capture's
GPS (a phone's), the nearest region; else it is "elsewhere".

``source="automatic"`` counts the automatic lengths instead (the owner,
2026-11-08: a toggle between the human and the automatic labels): the
automatic lengths list's (`automatic_lengths_store`), current and on no frame
the slate detector calls a slate, placed by the same rules from the frame's
own GPS. They are unvalidated, and the page says so. A frame a person
labeled a fish model is left off, as on the human map. A fish model nobody
labeled cannot be told from a wild fish, so a pool's lengths are left off
whole: the lab's map showed 11 fish in Southern California, unlabeled frames
of a Nathan's Pool trial (mission 509) whose 162 labeled fish were all fish
models (the owner, 2026-11-10). The human map keeps a pool's wild fish.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.deployment_inference import place_of
from fishsense_services_api.regions import (
    REGIONS,
    is_pool,
    nearest_region,
    region_of_place,
)
from fishsense_services_api.slate_presence_store import detector_slate_frame

__all__ = ["Source", "TenantRegion", "TenantRegions", "combine", "tenant_regions"]

#: Whose labels the map counts.
Source = Literal["human", "automatic"]

_HUMAN_SQL = """
    SELECT m.source_path, o.latitude, o.longitude, count(*) AS fish
    FROM observations o
    LEFT JOIN missions m
      ON m.tenant_id = o.tenant_id AND m.id = o.mission_id
    WHERE o.tenant_id = :t AND o.fish_model IS NULL
    GROUP BY m.source_path, o.latitude, o.longitude
"""

#: The automatic lengths, by the rules of the view `current_automatic_measurements`
#: (0081), restated here rather than read through the view. Through the view,
#: the lab's map ran past the app role's 30 s statement timeout and the page
#: showed a 500 (2026-11-09, v0.78.0; over 120 s by hand). Postgres expected one
#: row, so it re-ran the view's per-mission calibration pick
#: (`automatic_measurement_calibrations`) for each of the lab's 70,378
#: measurement rows. Here the pick is read once per tenant (``mc``, 203 rows for
#: the lab), and each step is materialized so the planner cannot nest-loop them
#: again. It took 5.7 s, and on production it picked the view's row for all
#: 27,313 of the lab's current frames. The rules: a frame's current length is its
#: newest measurement whose head/tail is the frame's newest prediction, whose
#: calibration is its mission's current pick, and whose aim is the frame's beam
#: aim. A change to the view must change this too: tests/test_tenant_regions.py
#: pins the view.
_AUTOMATIC_SQL = f"""
    WITH mc AS MATERIALIZED (
        SELECT mc.mission_id, mc.calibration_source,
               mc.automatic_laser_calibration_id, mc.laser_calibration_id,
               x.aim
        FROM automatic_measurement_calibrations mc
        LEFT JOIN automatic_laser_calibrations x
          ON x.tenant_id = mc.tenant_id
         AND x.id = mc.automatic_laser_calibration_id
        WHERE mc.tenant_id = :t
    ),
    am AS MATERIALIZED (
        SELECT DISTINCT ON (m.capture_id) m.capture_id, m.length_m
        FROM automatic_measurements m
        JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
        JOIN LATERAL (
            SELECT h.id FROM automatic_head_tail_predictions h
            WHERE h.tenant_id = m.tenant_id AND h.capture_id = m.capture_id
            ORDER BY h.seq DESC
            LIMIT 1
        ) ht ON ht.id = m.automatic_head_tail_prediction_id
        JOIN mc ON mc.mission_id = c.mission_id
         AND mc.calibration_source = m.calibration_source
         AND NOT mc.automatic_laser_calibration_id
                 IS DISTINCT FROM m.automatic_laser_calibration_id
         AND NOT mc.laser_calibration_id IS DISTINCT FROM m.laser_calibration_id
         AND beam_aim(c.tenant_id, c.mission_id, c.captured_at) = mc.aim
        WHERE m.tenant_id = :t
        ORDER BY m.capture_id, m.seq DESC
    ),
    fish_model AS MATERIALIZED (
        SELECT DISTINCT o.capture_id FROM observations o
        WHERE o.tenant_id = :t AND o.fish_model IS NOT NULL
    )
    SELECT d.source_path, c.latitude, c.longitude, count(*) AS fish
    FROM am
    JOIN captures c ON c.tenant_id = :t AND c.id = am.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE am.length_m IS NOT NULL
      AND NOT {detector_slate_frame()}
      AND NOT EXISTS (SELECT 1 FROM fish_model f WHERE f.capture_id = c.id)
    GROUP BY d.source_path, c.latitude, c.longitude
"""


@dataclass(frozen=True)
class TenantRegion:
    name: str
    latitude: float
    longitude: float
    fish: int


@dataclass(frozen=True)
class TenantRegions:
    #: Most fish first.
    regions: list[TenantRegion]
    #: Wild fish no region holds.
    elsewhere: int


async def tenant_regions(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, source: Source = "human"
) -> TenantRegions:
    # One row per folder and position, not per fish: the regions are worked
    # out here, from rules kept in Python (`place_of`).
    sql = _AUTOMATIC_SQL if source == "automatic" else _HUMAN_SQL
    rows = await conn.execute(text(sql), {"t": tenant_id})
    by_region: Counter[str] = Counter()
    elsewhere = 0
    for row in rows:
        place = place_of(row.source_path) if row.source_path else None
        if source == "automatic" and is_pool(place):
            continue
        region = region_of_place(place)
        if region is None and row.latitude is not None and row.longitude is not None:
            region = nearest_region(row.latitude, row.longitude)
        if region is None:
            elsewhere += row.fish
        else:
            by_region[region] += row.fish
    return _regions(by_region, elsewhere)


def combine(maps: Iterable[TenantRegions]) -> TenantRegions:
    """Several tenants' maps as one: each region's fish added up by name, as
    the system admins' map of the whole dataset needs (the owner,
    2026-11-11)."""
    by_region: Counter[str] = Counter()
    elsewhere = 0
    for found in maps:
        for region in found.regions:
            by_region[region.name] += region.fish
        elsewhere += found.elsewhere
    return _regions(by_region, elsewhere)


def _regions(by_region: Counter[str], elsewhere: int) -> TenantRegions:
    return TenantRegions(
        regions=[
            TenantRegion(name, *REGIONS[name], fish=fish)
            for name, fish in sorted(by_region.items(), key=lambda r: (-r[1], r[0]))
        ],
        elsewhere=elsewhere,
    )
