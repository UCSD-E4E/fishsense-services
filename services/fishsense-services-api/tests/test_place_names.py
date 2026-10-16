"""Naming a capture's place from its coordinates, on our own geocoder.

FishSense data is often collected with no internet, so the app frequently
can't name the place when a capture is taken, and a committed capture is
immutable (fishsense-mobile #72). The server derives the name instead, from
the coordinates every capture carries -- on a **self-hosted** Photon
geocoder (owner decision, 2026-10-16): a partner's capture locations (their
fishing spots) never leave our infrastructure.
"""

import pytest

from fishsense_services_api.place_names import (
    GeocoderUnavailable,
    PhotonGeocoder,
)
from photon_server import PhotonServer, feature

LA_JOLLA = (32.8506, -117.2717)


@pytest.fixture
def photon():
    with PhotonServer() as server:
        yield server


def _geocoder(photon, **options) -> PhotonGeocoder:
    return PhotonGeocoder(photon.base_url, **options)


async def test_a_place_is_named_from_the_nearest_feature(photon):
    photon.features = [
        feature(
            32.8506,
            -117.2717,
            name="La Jolla Cove",
            city="San Diego",
            state="California",
            country="United States",
        )
    ]

    found = await _geocoder(photon).reverse(*LA_JOLLA)

    assert found.place_name == "La Jolla Cove, San Diego, California, United States"
    assert found.properties["name"] == "La Jolla Cove"


async def test_a_part_named_twice_is_named_once(photon):
    photon.features = [
        feature(
            32.85, -117.27, name="San Diego", city="San Diego", country="United States"
        )
    ]

    found = await _geocoder(photon).reverse(*LA_JOLLA)

    assert found.place_name == "San Diego, United States"


async def test_a_feature_far_from_the_capture_is_named_as_near(photon):
    """At sea, the nearest named place can be kilometres away."""
    photon.features = [
        feature(
            32.70, -117.16, name="Coronado", state="California", country="United States"
        )
    ]

    found = await _geocoder(photon).reverse(*LA_JOLLA)

    assert found.place_name == "near Coronado, California, United States"


async def test_no_feature_in_range_is_no_name(photon):
    found = await _geocoder(photon).reverse(*LA_JOLLA)

    assert found.place_name is None


async def test_the_query_asks_for_one_nearby_feature_in_english(photon):
    await _geocoder(photon, radius_km=25).reverse(*LA_JOLLA)

    assert photon.queries == [
        {
            "lat": "32.8506",
            "lon": "-117.2717",
            "limit": "1",
            "lang": "en",
            "radius": "25",
        }
    ]


@pytest.mark.parametrize("status", [500, 503])
async def test_a_failing_geocoder_is_unavailable_not_no_name(photon, status):
    """An outage must not be recorded as "this place has no name"."""
    photon.status = status

    with pytest.raises(GeocoderUnavailable):
        await _geocoder(photon).reverse(*LA_JOLLA)


async def test_a_slow_geocoder_is_unavailable(photon):
    photon.delay_seconds = 1.0

    with pytest.raises(GeocoderUnavailable):
        await _geocoder(photon, timeout_seconds=0.2).reverse(*LA_JOLLA)


async def test_an_unreachable_geocoder_is_unavailable():
    with pytest.raises(GeocoderUnavailable):
        await PhotonGeocoder("http://127.0.0.1:9").reverse(*LA_JOLLA)
