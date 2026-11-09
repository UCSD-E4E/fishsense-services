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
labeled a fish model is left off, as on the human map; a fish model nobody
labeled cannot be told from a wild fish, so it stays on.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.deployment_inference import place_of
from fishsense_services_api.regions import REGIONS, nearest_region, region_of_place
from fishsense_services_api.slate_presence_store import detector_slate_frame

__all__ = ["Source", "TenantRegion", "TenantRegions", "tenant_regions"]

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

_AUTOMATIC_SQL = f"""
    SELECT d.source_path, c.latitude, c.longitude, count(*) AS fish
    FROM current_automatic_measurements am
    JOIN captures c ON c.tenant_id = am.tenant_id AND c.id = am.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE am.tenant_id = :t AND am.length_m IS NOT NULL
      AND NOT {detector_slate_frame()}
      AND NOT EXISTS (
          SELECT 1 FROM observations o
          WHERE o.tenant_id = c.tenant_id AND o.capture_id = c.id
            AND o.fish_model IS NOT NULL)
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
        region = region_of_place(place_of(row.source_path)) if row.source_path else None
        if region is None and row.latitude is not None and row.longitude is not None:
            region = nearest_region(row.latitude, row.longitude)
        if region is None:
            elsewhere += row.fish
        else:
            by_region[region] += row.fish
    return TenantRegions(
        regions=[
            TenantRegion(name, *REGIONS[name], fish=fish)
            for name, fish in sorted(by_region.items(), key=lambda r: (-r[1], r[0]))
        ],
        elsewhere=elsewhere,
    )
