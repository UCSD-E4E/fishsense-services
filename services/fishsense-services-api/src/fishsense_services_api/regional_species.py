"""Regional species: the fish BioCLIP may also name, by where a mission was
(0085; the owner, 2026-10-30).

A mission's region is its folder's place (`deployment_inference.place_of`)
in `regions`, a pool at its region; a mission in none is offered nothing
here, so BioCLIP chooses among the 13 target species alone, as before.

When its own folder names no place, walk up the tree (the owner,
2026-11-12): 22 of the 481 REEF missions named none, folders like
``092223_FSL-03 Photos``. Then, in order, and only when what they say agrees
on one region: another copy of its files (the same checksum) in a folder
naming a place, as ``09_2023_Islamorada`` does for that one; its
deployment's name; its deployment's other missions whose folders' dates
agree with their photos'. A reef mission still in
none is in :data:`UNKNOWN_REEF_REGION`, which has no fish of its own, so it
is offered the REEF targets alone (the owner, the same day); recording the
name says it was looked for. Paths in the database only, never the NAS. The
orchestrator adds a region's fish to the targets; a species row records the
region it was offered and :data:`SPECIES_LIST_VERSION`, and a row from
another version is owed again (`automatic_results_store`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.deployment_inference import folder_date, place_of
from fishsense_services_api.regions import region_of_place

__all__ = [
    "SPECIES_LIST_VERSION",
    "SpeciesList",
    "UNKNOWN_REEF_REGION",
    "automatic_species_list",
    "mission_species_region",
]

#: Bumped by hand whenever what a mission is offered changes: a list's
#: species, or how a mission finds its region. 1 (2026-10-30): 0085's lists.
SPECIES_LIST_VERSION = 1


#: A reef mission nothing places (the owner, 2026-11-12): the targets alone.
UNKNOWN_REEF_REGION = "Unknown REEF Region"

#: The tenant whose missions with no place are in :data:`UNKNOWN_REEF_REGION`.
_REEF = "reef"


@dataclass(frozen=True)
class SpeciesList:
    #: None: the mission is in no region.
    region: str | None
    #: (common name, scientific name), most recorded first.
    species: list[tuple[str, str]]


def _agreed(paths) -> str | None:
    """The one region these paths' places say, or None (none, or two)."""
    regions = {region_of_place(place_of(p or "")) for p in paths} - {None}
    return regions.pop() if len(regions) == 1 else None


def _dated_alike(row) -> bool:
    """A mission's folder and photos agree on the day, or nothing says
    otherwise. Production, 2026-11-12: mission 398's folder says Alligator on
    2024-08-20 and its photos 2024-07-09, so the deployments grouped it with
    July's dives (399-401), and through it they were placed in the Keys."""
    day = folder_date(row.source_path or "")
    return day is None or row.first is None or row.first <= day <= row.last


async def mission_species_region(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> str | None:
    params = {"t": tenant_id, "d": mission_id}
    row = (
        await conn.execute(
            text("""
                SELECT m.source_path, m.deployment_id, d.name AS deployment,
                       t.slug
                FROM missions m
                JOIN tenants t ON t.id = m.tenant_id
                LEFT JOIN deployments d
                  ON d.tenant_id = m.tenant_id AND d.id = m.deployment_id
                WHERE m.tenant_id = :t AND m.id = :d
                """),
            params,
        )
    ).one()
    region = region_of_place(place_of(row.source_path or ""))
    if region is not None:
        return region
    copies = await conn.execute(
        text("""
            SELECT DISTINCT o.source_path
            FROM captures c
            JOIN captures c2
              ON c2.tenant_id = c.tenant_id AND c2.checksum = c.checksum
             AND c2.mission_id <> c.mission_id
            JOIN missions o ON o.tenant_id = c2.tenant_id AND o.id = c2.mission_id
            WHERE c.tenant_id = :t AND c.mission_id = :d
            """),
        params,
    )
    region = _agreed(r.source_path for r in copies)
    if region is None and row.deployment_id is not None:
        region = region_of_place(place_of(row.deployment or ""))
        if region is None:
            others = await conn.execute(
                text("""
                    SELECT o.source_path, min(c.captured_at)::date AS first,
                           max(c.captured_at)::date AS last
                    FROM missions o
                    LEFT JOIN captures c
                      ON c.tenant_id = o.tenant_id AND c.mission_id = o.id
                    WHERE o.tenant_id = :t AND o.deployment_id = :dep AND o.id <> :d
                    GROUP BY o.id, o.source_path
                    """),
                {**params, "dep": row.deployment_id},
            )
            region = _agreed(r.source_path for r in others if _dated_alike(r))
    if region is None and row.slug == _REEF:
        return UNKNOWN_REEF_REGION
    return region


async def automatic_species_list(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> SpeciesList:
    region = await mission_species_region(conn, tenant_id, mission_id)
    if region is None:
        return SpeciesList(None, [])
    rows = await conn.execute(
        text("""
            SELECT common_name, scientific_name FROM regional_species
            WHERE region = :r ORDER BY rank
            """),
        {"r": region},
    )
    return SpeciesList(region, [(r.common_name, r.scientific_name) for r in rows])
