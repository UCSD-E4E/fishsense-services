"""Regional species: the fish BioCLIP may also name, by where a mission was
(0085; the owner, 2026-10-30).

A mission's region is its folder's place (`deployment_inference.place_of`)
in `regions`, a pool at its region; a mission in none is offered nothing
here, so BioCLIP chooses among the 13 target species alone, as before. The
orchestrator adds a region's fish to the targets; a species row records the
region it was offered and :data:`SPECIES_LIST_VERSION`, and a row from
another version is owed again (`automatic_results_store`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.deployment_inference import place_of
from fishsense_services_api.regions import region_of_place

__all__ = [
    "SPECIES_LIST_VERSION",
    "SpeciesList",
    "automatic_species_list",
    "mission_species_region",
]

#: Bumped by hand whenever what a mission is offered changes: a list's
#: species, or how a mission finds its region. 1 (2026-10-30): 0085's lists.
SPECIES_LIST_VERSION = 1


@dataclass(frozen=True)
class SpeciesList:
    #: None: the mission is in no region.
    region: str | None
    #: (common name, scientific name), most recorded first.
    species: list[tuple[str, str]]


async def mission_species_region(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> str | None:
    source_path = (
        await conn.execute(
            text("SELECT source_path FROM missions WHERE tenant_id = :t AND id = :d"),
            {"t": tenant_id, "d": mission_id},
        )
    ).scalar_one()
    return region_of_place(place_of(source_path or ""))


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
