"""What the signed-out page may say (docs/design/README.md, Home pages; the
owner, 2026-10-20: "very broad so that we don't reveal anything").

From tenants that opted in alone (0055), it gives live totals and wild fish by
**broad region**: a mission's folder names a place (`deployment_inference`'s
`place_of`, the rules `infer-deployments` uses), and a place belongs to a region
here. A region shows a count only once it holds `MINIMUM` wild fish, rounded to
the nearest 10; below that it says "under `MINIMUM`". A place no region holds,
or a folder that names none, counts in the totals and not on the map. Fish
models, decoys measured in pool trials, are never on the map.

Nothing finer than a region ever leaves here: not a place, a folder, a tenant,
or an exact count.
"""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.deployment_inference import place_of

__all__ = ["MINIMUM", "PublicRegion", "PublicSummary", "REGIONS", "public_summary"]

#: A region shows its count from this many wild fish (the owner, 2026-10-20).
MINIMUM = 25

#: Each region's place on the map: broad, never a site.
REGIONS: dict[str, tuple[float, float]] = {
    "Florida Keys, USA": (27.0, -81.5),
    "Caribbean": (18.5, -76.0),
    "Southern California, USA": (33.5, -117.5),
}

#: The places `place_of` names, and their regions. A pool is nowhere in public.
_REGION_OF = {
    "Islamorada": "Florida Keys, USA",
    "Key Largo": "Florida Keys, USA",
    "Cuba": "Caribbean",
    "Cayman Islands": "Caribbean",
    "St Lucia": "Caribbean",
    "Curaçao": "Caribbean",
    "San Diego": "Southern California, USA",
}


@dataclass(frozen=True)
class PublicRegion:
    name: str
    latitude: float
    longitude: float
    #: Rounded to the nearest 10; None below `MINIMUM`.
    count: int | None
    #: `MINIMUM` when the count is hidden, so the page can say "under 25".
    under: int | None


@dataclass(frozen=True)
class PublicSummary:
    lengths: int
    wild_fish: int
    model_lengths: int
    species: int
    missions: int
    deployments: int
    regions: list[PublicRegion]


def _region(name: str, wild: int) -> PublicRegion:
    latitude, longitude = REGIONS[name]
    if wild < MINIMUM:
        return PublicRegion(name, latitude, longitude, count=None, under=MINIMUM)
    return PublicRegion(name, latitude, longitude, count=round(wild, -1), under=None)


async def public_summary(conn: AsyncConnection) -> PublicSummary:
    totals = (await conn.execute(text("SELECT * FROM public_totals()"))).one()
    by_region: dict[str, int] = defaultdict(int)
    for row in await conn.execute(text("SELECT * FROM public_wild_fish_by_folder()")):
        region = _REGION_OF.get(place_of(row.source_path) or "")
        if region:
            by_region[region] += row.wild_fish
    regions = sorted(
        (_region(name, wild) for name, wild in by_region.items()),
        key=lambda r: (-(r.count or 0), r.name),
    )
    return PublicSummary(**totals._mapping, regions=regions)
