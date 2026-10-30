"""A capture's place name, derived from its coordinates on our own geocoder.

FishSense data is often collected with no internet, so the app frequently
can't name the place when a capture is taken, and a committed capture is
immutable (fishsense-mobile #72). The server names it instead, from the
coordinates every mobile capture carries.

**Self-hosted** (owner decision, 2026-10-16): the geocoder is a Photon
instance krg-infra runs (docs/handoff/krg-infra/GEOCODER.md), so a partner's
capture locations -- their fishing spots -- never leave our infrastructure.
Photon's reverse API: ``GET /reverse?lat=&lon=&limit=1&lang=en&radius=<km>``
answers the nearest OpenStreetMap feature as GeoJSON.

The name is "<feature>, <town>, <state>, <country>", each part once; a
feature more than a kilometer from the capture (at sea, the nearest named
place can be far) is "near <...>". A derived name never replaces what the app
sent; it is kept apart, with the geocoder's answer, so it can be told apart
and re-derived.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import httpx

__all__ = [
    "Geocoder",
    "GeocoderUnavailable",
    "PhotonGeocoder",
    "ReverseGeocode",
]

#: The source recorded with each derived name.
PHOTON = "photon"
#: Farther than this from the capture, the feature is named as "near".
NEAR_METRES = 1000.0
_EARTH_RADIUS_METRES = 6_371_000.0
#: Photon's address parts, coarsest last, after the feature's own name.
_PARTS = ("name", "city", "district", "locality", "county", "state", "country")


class GeocoderUnavailable(Exception):
    """The geocoder didn't answer: an outage, never "no name here"."""


@dataclass(frozen=True)
class ReverseGeocode:
    #: None when no named feature is within range.
    place_name: str | None
    source: str
    #: The feature's properties, as the geocoder gave them.
    properties: dict = field(default_factory=dict)


class Geocoder(Protocol):
    async def reverse(self, latitude: float, longitude: float) -> ReverseGeocode: ...


class PhotonGeocoder:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 3.0,
        radius_km: float = 25.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/reverse"
        self._timeout = timeout_seconds
        self._radius_km = radius_km

    async def reverse(self, latitude: float, longitude: float) -> ReverseGeocode:
        params = {
            "lat": f"{latitude}",
            "lon": f"{longitude}",
            "limit": "1",
            "lang": "en",
            "radius": f"{self._radius_km:g}",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._url, params=params)
            response.raise_for_status()
            features = response.json().get("features") or []
        except (httpx.HTTPError, ValueError) as error:
            raise GeocoderUnavailable(str(error) or type(error).__name__) from error
        if not features:
            return ReverseGeocode(place_name=None, source=PHOTON)
        found = features[0]
        properties = found.get("properties") or {}
        return ReverseGeocode(
            place_name=_name(properties, latitude, longitude, found.get("geometry")),
            source=PHOTON,
            properties=properties,
        )


def _name(properties: dict, latitude: float, longitude: float, geometry) -> str | None:
    parts: list[str] = []
    for key in _PARTS:
        value = properties.get(key)
        if isinstance(value, str) and value and value not in parts:
            parts.append(value)
    if not parts:
        return None
    name = ", ".join(parts)
    try:
        lon, lat = geometry["coordinates"][:2]
    except (TypeError, KeyError, ValueError):
        return name
    if _metres(latitude, longitude, lat, lon) > NEAR_METRES:
        return f"near {name}"
    return name


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_METRES * math.asin(math.sqrt(a))
