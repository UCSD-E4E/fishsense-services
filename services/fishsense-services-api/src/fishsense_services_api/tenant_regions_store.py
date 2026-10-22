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
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.deployment_inference import place_of
from fishsense_services_api.regions import REGIONS, nearest_region, region_of_place

__all__ = ["TenantRegion", "TenantRegions", "tenant_regions"]


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


async def tenant_regions(conn: AsyncConnection, tenant_id: uuid.UUID) -> TenantRegions:
    # One row per folder and position, not per fish: the regions are worked
    # out here, from rules kept in Python (`place_of`).
    rows = await conn.execute(
        text("""
            SELECT m.source_path, o.latitude, o.longitude, count(*) AS fish
            FROM observations o
            LEFT JOIN missions m
              ON m.tenant_id = o.tenant_id AND m.id = o.mission_id
            WHERE o.tenant_id = :t AND o.fish_model IS NULL
            GROUP BY m.source_path, o.latitude, o.longitude
            """),
        {"t": tenant_id},
    )
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
