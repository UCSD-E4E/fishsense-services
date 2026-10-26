"""Observations over HTTP: every measured fish, Lite and Mobile alike.

``GET /tenants/{slug}/observations`` and
``GET /tenants/{slug}/observations/{capture_id}/{fish_id}``, for any member of
the tenant (PLAN.md §4.9, Phase 1 of §9.26). One list for both devices,
newest first; ``?device=lite|mobile`` narrows it. A mobile fish carries a
presigned link to its photo; a Lite fish's is its capture's render, once one
is in the tenant's bucket (Phase 4, 0051). The detail carries the evidence of
its method.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from depth_measure_seed import T0, exec_, forget_identities  # noqa: F401
from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.tenant_stores import TenantStore, TenantStores
from observation_seed import ALICE, lite, mobile, render, thumbnail

ISSUER = "https://auth.example.test/application/o/fishsense/"
AUDIENCE = "fishsense-web"
KID = "k1"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OUTSIDER = "sub-outsider"


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member", "partner": "member"}})


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    # Presigning is computed locally: no Garage needed to sign a link.
    stores = TenantStores(
        endpoint_url="http://garage.test:3900",
        public_endpoint_url="http://garage.test:3900",
        region="garage",
        stores={"lab": TenantStore("fishsense-tenant-lab", "GK" + "0" * 24, "0" * 64)},
    )
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )
    app = create_app(engine=app_engine, validator=validator, stores=stores)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


async def _get(client, path: str, sub: str = ALICE, **params) -> httpx.Response:
    return await client.get(path, headers=_bearer(sub), params=params)


class TestList:
    async def test_a_member_sees_lite_and_mobile_fish_together(
        self, client, owner_engine, app_engine, tenants
    ):
        lite_capture, lite_fish = await lite(
            owner_engine, tenants["lab"], model=("Ginny", 0.3127)
        )
        phone_capture, client_uuid = await mobile(
            app_engine, owner_engine, tenants["lab"], at=T0 + timedelta(days=1)
        )

        response = await _get(client, "/tenants/lab/observations")

        assert response.status_code == 200
        body = response.json()
        phone, decoy = body["observations"]
        assert body["next"] is None

        assert phone["capture_id"] == str(phone_capture)
        assert phone["method"] == "lidar"
        assert phone["device"] == {"kind": "mobile", "name": None}
        assert phone["client_capture_uuid"] == str(client_uuid)
        assert phone["place_name"] == "Scripps Pier"
        assert phone["location"] == {"latitude": 32.8665, "longitude": -117.2546}
        assert phone["photo_url"].startswith(
            "http://garage.test:3900/fishsense-tenant-lab/mobile/captures/"
        )
        # Its phone mission (0057), named for the day and the place.
        assert phone["mission"]["name"] == "2025.03.07.FishSense.Scripps Pier"

        assert (decoy["capture_id"], decoy["fish_id"]) == (
            str(lite_capture),
            str(lite_fish),
        )
        assert decoy["method"] == "laser"
        assert decoy["device"] == {"kind": "lite", "name": "FSL-07"}
        assert decoy["mission"]["name"] == "Alligator Deep"
        assert decoy["fish_model"] == {"name": "Ginny", "known_length_m": 0.3127}
        assert decoy["species"] is None
        assert decoy["photo_url"] is None
        assert decoy["length_m"] == pytest.approx(0.305)

    async def test_a_lite_fish_shows_its_render_from_the_tenants_bucket(
        self, client, owner_engine, app_engine, tenants
    ):
        capture_id, fish_id = await lite(owner_engine, tenants["lab"])
        key = await render(owner_engine, tenants["lab"], capture_id)
        thumb = await thumbnail(owner_engine, tenants["lab"], key)

        [listed] = (await _get(client, "/tenants/lab/observations")).json()[
            "observations"
        ]
        detail = (
            await _get(client, f"/tenants/lab/observations/{capture_id}/{fish_id}")
        ).json()

        base = "http://garage.test:3900/fishsense-tenant-lab/"
        # The card is the thumbnail; the fish's page, the render itself.
        assert listed["photo_url"].startswith(base + thumb)
        assert detail["observation"]["photo_url"].startswith(base + key)

    async def test_a_render_in_another_bucket_is_not_signed_for_this_one(
        self, client, owner_engine, app_engine, tenants
    ):
        """A presign names the tenant's bucket; a render recorded in another
        (the tenant moved buckets since) would be a link to nothing."""
        capture_id, _ = await lite(owner_engine, tenants["lab"])
        await render(owner_engine, tenants["lab"], capture_id, bucket="old-lab-bucket")
        await render(
            owner_engine, tenants["lab"], capture_id, version=2, bucket="other-bucket"
        )

        [listed] = (await _get(client, "/tenants/lab/observations")).json()[
            "observations"
        ]

        assert listed["photo_url"] is None

    @pytest.mark.parametrize("device, method", [("lite", "laser"), ("mobile", "lidar")])
    async def test_device_narrows_the_list(
        self, client, owner_engine, app_engine, tenants, device, method
    ):
        await lite(owner_engine, tenants["lab"], species=("A b", "C"))
        await mobile(app_engine, owner_engine, tenants["lab"])

        response = await _get(client, "/tenants/lab/observations", device=device)

        assert [o["method"] for o in response.json()["observations"]] == [method]

    async def test_pages_follow_next(self, client, owner_engine, app_engine, tenants):
        for h in range(3):
            await mobile(
                app_engine, owner_engine, tenants["lab"], at=T0 + timedelta(hours=h)
            )

        first = (await _get(client, "/tenants/lab/observations", limit=2)).json()
        second = (
            await _get(
                client, "/tenants/lab/observations", limit=2, before=first["next"]
            )
        ).json()

        assert len(first["observations"]) == 2
        assert len(second["observations"]) == 1
        assert second["next"] is None

    async def test_pages_by_number_say_how_many_there_are(
        self, client, owner_engine, app_engine, tenants
    ):
        for h in range(5):
            await mobile(
                app_engine, owner_engine, tenants["lab"], at=T0 + timedelta(hours=h)
            )

        pages = [
            (await _get(client, "/tenants/lab/observations", limit=2, page=n)).json()
            for n in (1, 2, 3, 4)
        ]

        assert [len(p["observations"]) for p in pages] == [2, 2, 1, 0]
        assert [p["total"] for p in pages] == [5, 5, 5, 5]
        seen = [o["capture_id"] for p in pages for o in p["observations"]]
        assert len(set(seen)) == 5

    async def test_the_total_comes_with_every_list(self, client, owner_engine, tenants):
        await lite(owner_engine, tenants["lab"], species=("A b", "C"))

        listed = (await _get(client, "/tenants/lab/observations")).json()

        assert listed["total"] == 1

    async def test_a_page_that_is_not_one_is_refused(self, client, tenants):
        response = await _get(client, "/tenants/lab/observations", page=0)

        assert response.status_code == 422

    async def test_a_page_and_a_cursor_together_are_refused(
        self, client, owner_engine, app_engine, tenants
    ):
        """Two places to start from: which one meant is anyone's guess."""
        for h in range(3):
            await mobile(
                app_engine, owner_engine, tenants["lab"], at=T0 + timedelta(hours=h)
            )
        cursor = (await _get(client, "/tenants/lab/observations", limit=1)).json()[
            "next"
        ]

        response = await _get(
            client, "/tenants/lab/observations", limit=1, page=2, before=cursor
        )

        assert response.status_code == 422

    async def test_a_cursor_that_is_not_one_is_refused(self, client, tenants):
        response = await _get(client, "/tenants/lab/observations", before="nonsense")

        assert response.status_code == 422

    async def test_someone_who_is_not_a_member_gets_nothing(
        self, client, owner_engine, tenants
    ):
        await lite(owner_engine, tenants["lab"], species=("A b", "C"))

        response = await _get(client, "/tenants/lab/observations", sub=OUTSIDER)

        assert response.status_code == 404

    async def test_a_request_without_a_token_is_refused(self, client, tenants):
        response = await client.get("/tenants/lab/observations")

        assert response.status_code == 401


class TestRegions:
    """Where the tenant's fish were measured, by broad region (Today's map)."""

    async def test_a_member_sees_their_tenants_regions(
        self, client, owner_engine, app_engine, tenants
    ):
        await mobile(app_engine, owner_engine, tenants["lab"])  # La Jolla
        await lite(owner_engine, tenants["lab"], species=("A b", "C"))  # no place

        response = await _get(client, "/tenants/lab/regions")

        assert response.status_code == 200
        assert response.json() == {
            "regions": [
                {
                    "name": "Southern California, USA",
                    "latitude": 33.5,
                    "longitude": -117.5,
                    "fish": 1,
                }
            ],
            "elsewhere": 1,
        }

    async def test_someone_who_is_not_a_member_gets_nothing(self, client, tenants):
        response = await _get(client, "/tenants/lab/regions", sub=OUTSIDER)

        assert response.status_code == 404


class TestDetail:
    async def test_a_lite_fish_shows_its_laser_evidence(
        self, client, owner_engine, tenants
    ):
        capture_id, fish_id = await lite(
            owner_engine, tenants["lab"], model=("Ginny", 0.3127)
        )

        response = await _get(
            client, f"/tenants/lab/observations/{capture_id}/{fish_id}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["observation"]["method"] == "laser"
        assert body["laser"] == {
            "depth_m": 1.8,
            "range_m": 1.8,
            "calibration_producer": "slate",
            "dot": {"x": 1925.0, "y": 1310.0},
            "head": {"x": 1600.0, "y": 1125.0},
            "tail": {"x": 2200.0, "y": 1360.0},
            # The seed's label names no colour.
            "color": None,
        }
        assert body["device_points"] is None
        assert body["algorithm"] == "laser_depth_fronto_parallel"

    async def test_a_rendered_lite_fish_says_how_big_its_photo_is(
        self, client, owner_engine, tenants
    ):
        """The page draws the labelled points on the render at their pixels
        (the render is never cropped: the processor's whole-frame test), so
        it needs the render's size to place them."""
        capture_id, fish_id = await lite(
            owner_engine, tenants["lab"], model=("Ginny", 0.3127)
        )
        await render(owner_engine, tenants["lab"], capture_id)

        body = (
            await _get(client, f"/tenants/lab/observations/{capture_id}/{fish_id}")
        ).json()

        assert body["photo_size"] == {"width": 4014, "height": 3016}

    async def test_without_a_render_there_is_no_photo_size(
        self, client, owner_engine, app_engine, tenants
    ):
        capture_id, fish_id = await lite(
            owner_engine, tenants["lab"], model=("Ginny", 0.3127)
        )
        await mobile(app_engine, owner_engine, tenants["lab"])
        phone = next(
            o
            for o in (await _get(client, "/tenants/lab/observations")).json()[
                "observations"
            ]
            if o["method"] == "lidar"
        )

        lite_body = (
            await _get(client, f"/tenants/lab/observations/{capture_id}/{fish_id}")
        ).json()
        phone_body = (
            await _get(
                client,
                f"/tenants/lab/observations/{phone['capture_id']}/{phone['fish_id']}",
            )
        ).json()

        assert lite_body["photo_size"] is None
        # A phone's photo is its own, and its page sizes it from the capture.
        assert phone_body["photo_size"] is None

    async def test_a_mobile_fish_shows_the_phones_points(
        self, client, owner_engine, app_engine, tenants
    ):
        await mobile(app_engine, owner_engine, tenants["lab"])
        [listed] = (await _get(client, "/tenants/lab/observations")).json()[
            "observations"
        ]

        response = await _get(
            client,
            f"/tenants/lab/observations/{listed['capture_id']}/{listed['fish_id']}",
        )

        body = response.json()
        assert body["device_points"] == {
            "snout": {"x": 100.0, "y": 200.0},
            "fork": {"x": 900.0, "y": 210.0},
            "origin": "manual",
        }
        assert body["laser"] is None

    async def test_another_tenants_fish_is_not_found(
        self, client, owner_engine, tenants
    ):
        capture_id, fish_id = await lite(
            owner_engine, tenants["partner"], species=("A b", "C")
        )

        response = await _get(
            client, f"/tenants/lab/observations/{capture_id}/{fish_id}"
        )

        assert response.status_code == 404

    async def test_an_unknown_fish_is_not_found(self, client, tenants):
        response = await _get(
            client, f"/tenants/lab/observations/{uuid.uuid4()}/{uuid.uuid4()}"
        )

        assert response.status_code == 404


class TestKindsOfFish:
    async def test_a_species_known_only_by_its_common_name_is_still_named(
        self, client, owner_engine, tenants
    ):
        capture_id, fish_id = await lite(owner_engine, tenants["lab"])
        await exec_(
            owner_engine,
            "INSERT INTO species (scientific_name, common_name) VALUES "
            "(NULL, 'Garibaldi')",
        )
        await exec_(
            owner_engine,
            "UPDATE fish SET species_id = (SELECT id FROM species WHERE "
            "common_name = 'Garibaldi') WHERE id = :f",
            f=fish_id,
        )

        [found] = (await _get(client, "/tenants/lab/observations")).json()[
            "observations"
        ]

        assert found["species"] == {"scientific_name": None, "common_name": "Garibaldi"}

    async def test_every_fish_is_under_exactly_one_device_filter(
        self, client, owner_engine, app_engine, tenants
    ):
        """Lite, Mobile, and everything else (other kinds, or no device):
        the filters partition the list, so their counts add up."""
        await lite(owner_engine, tenants["lab"], species=("A b", "C"))
        await mobile(app_engine, owner_engine, tenants["lab"])
        scout, _ = await lite(
            owner_engine, tenants["lab"], species=("A b", "C"), device_name="Scout-1"
        )
        await exec_(
            owner_engine,
            "UPDATE devices SET kind = 'scout' WHERE id = "
            "(SELECT device_id FROM captures WHERE id = :c)",
            c=scout,
        )

        def ids(body):
            return {o["capture_id"] for o in body["observations"]}

        everything = ids((await _get(client, "/tenants/lab/observations")).json())
        parts = [
            ids((await _get(client, "/tenants/lab/observations", device=d)).json())
            for d in ("lite", "mobile", "other")
        ]

        assert [len(p) for p in parts] == [1, 1, 1]
        assert set().union(*parts) == everything
        assert str(scout) in parts[2]


class TestSummary:
    async def test_a_member_reads_their_tenants_numbers(
        self, client, owner_engine, tenants
    ):
        await lite(owner_engine, tenants["lab"], species=("Scarus coeruleus", "Blue"))

        response = await _get(client, "/tenants/lab/summary")

        assert response.status_code == 200
        body = response.json()
        assert (body["lengths"], body["wild_fish"], body["species"]) == (1, 1, 1)
        assert body["missions"] == 1 and body["frames"] == 1
        assert body["latest_activity"] is not None

    async def test_someone_elses_tenant_is_not_found(self, client, tenants):
        response = await _get(client, "/tenants/lab/summary", sub=OUTSIDER)

        assert response.status_code == 404


class TestBySpecies:
    """The Fish page filters by species, and offers only the species the
    tenant has (the owner, 2026-10-23)."""

    async def _seed(self, owner_engine, tenants):
        await lite(
            owner_engine,
            tenants["lab"],
            species=("Lachnolaimus maximus", "Hogfish"),
            device_name="FSL-01",
        )
        await lite(
            owner_engine,
            tenants["lab"],
            species=("Scarus coeruleus", "Blue Parrotfish"),
            device_name="FSL-02",
            at=T0 + timedelta(days=1),
        )
        await lite(owner_engine, tenants["partner"], species=("Partneria only", "Other"),
                   device_name="FSL-03")  # fmt: skip

    async def test_the_list_keeps_one_species(self, client, owner_engine, tenants):
        await self._seed(owner_engine, tenants)

        response = await _get(
            client, "/tenants/lab/observations", species="Scarus coeruleus"
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert [o["species"]["scientific_name"] for o in body["observations"]] == [
            "Scarus coeruleus"
        ]
        assert body["total"] == 1

    async def test_the_species_offered_are_the_tenants_own(
        self, client, owner_engine, tenants
    ):
        await self._seed(owner_engine, tenants)

        response = await _get(client, "/tenants/lab/observations/species")

        assert response.status_code == 200, response.text
        assert response.json() == [
            {
                "scientific_name": "Lachnolaimus maximus",
                "common_name": "Hogfish",
                "count": 1,
            },
            {
                "scientific_name": "Scarus coeruleus",
                "common_name": "Blue Parrotfish",
                "count": 1,
            },
        ]

    async def test_they_follow_the_device_filter(self, client, owner_engine, tenants):
        await self._seed(owner_engine, tenants)

        response = await _get(
            client, "/tenants/lab/observations/species", device="mobile"
        )

        assert response.json() == []

    async def test_a_non_member_sees_none(self, client, owner_engine, tenants):
        await self._seed(owner_engine, tenants)

        response = await _get(client, "/tenants/lab/observations/species", sub=OUTSIDER)

        assert response.status_code == 404
