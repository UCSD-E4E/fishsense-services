"""Broad regions: where fish were measured, never finer.

A mission's folder names a place (`deployment_inference.place_of`), and a
place belongs to a region; a phone's fish, with GPS but no folder, joins the
nearest region within :data:`NEAREST_WITHIN_KM`. Each region has one point to
draw it at. Both maps use these: the public page's (`public_store`, field
places only) and a tenant's Today map (`tenant_regions_store`, its pools as
well, at their region: the owner, 2026-10-21).
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

__all__ = [
    "NEAREST_WITHIN_KM",
    "REGIONS",
    "is_pool",
    "nearest_region",
    "region_of_place",
    "region_of_site",
]

#: Each region's place on the map: broad, never a site.
REGIONS: dict[str, tuple[float, float]] = {
    "Florida Keys, USA": (27.0, -81.5),
    "Caribbean": (18.5, -76.0),
    "Southern California, USA": (33.5, -117.5),
}

#: The field places `place_of` names, and their regions.
_FIELD = {
    "Islamorada": "Florida Keys, USA",
    "Key Largo": "Florida Keys, USA",
    "Qui Lagoon": "Florida Keys, USA",  # the owner, 2026-11-09
    "Cuba": "Caribbean",
    "Cayman Islands": "Caribbean",
    "St Lucia": "Caribbean",
    "Curaçao": "Caribbean",
    "San Diego": "Southern California, USA",
}

#: The pools, at their region (the owner, 2026-10-21). Never on the public
#: page: a pool is nowhere in public.
_POOLS = {
    "Pool": "Florida Keys, USA",
    "Nathan's Pool": "Southern California, USA",
}

#: Mission sites (``missions.site``, a phone mission's) and their regions,
#: matched whole, ignoring case. CCFRP's phone captures have no GPS, and every
#: CCFRP photo is from San Diego (the owner, 2026-11-11): its 530 lengths were
#: all "elsewhere" on the automatic map (v0.87.0) until its three sites were
#: placed by name (the owner, 2026-11-12). Only these sites: a new one is
#: "elsewhere" until it is added here.
_SITES = {
    "oceanside sea center": "Southern California, USA",
    "sio": "Southern California, USA",
    "outer limits charters inc": "Southern California, USA",
}

#: How far a phone's fish may be from a region's point and still join it.
NEAREST_WITHIN_KM = 300.0

_EARTH_RADIUS_KM = 6371.0


def region_of_place(place: str | None, *, pools: bool = True) -> str | None:
    """The region a `place_of` place belongs to; None for none, or for a pool
    when ``pools`` is False."""
    if place is None:
        return None
    return _FIELD.get(place) or (_POOLS.get(place) if pools else None)


def region_of_site(site: str | None) -> str | None:
    """The region a mission's site is in, for the sites :data:`_SITES` names."""
    return _SITES.get((site or "").strip().casefold())


def is_pool(place: str | None) -> bool:
    """The place is a pool (a `place_of` place)."""
    return place in _POOLS


def nearest_region(latitude: float, longitude: float) -> str | None:
    """The region whose point is nearest, if within :data:`NEAREST_WITHIN_KM`."""
    name, (lat, lon) = min(
        REGIONS.items(), key=lambda r: _km(latitude, longitude, *r[1])
    )
    return name if _km(latitude, longitude, lat, lon) <= NEAREST_WITHIN_KM else None


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))
