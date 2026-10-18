"""Observations over HTTP: every measured fish, Lite and Mobile alike.

``GET /tenants/{slug}/observations`` and
``GET /tenants/{slug}/observations/{capture_id}/{fish_id}``, for any member of
the tenant (PLAN.md §4.9, Phase 1 of §9.26). One list for both devices,
newest first; ``?device=lite|mobile`` narrows it. A mobile fish carries a
presigned link to its photo; a Lite fish has none yet (its frames are not in
the tenant's bucket). The detail carries the evidence of its method.
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

from depth_measure_seed import T0, forget_identities  # noqa: F401  (a fixture)
from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.tenant_stores import TenantStore, TenantStores
from observation_seed import ALICE, lite, mobile

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
        assert phone["dive"] is None

        assert (decoy["capture_id"], decoy["fish_id"]) == (
            str(lite_capture),
            str(lite_fish),
        )
        assert decoy["method"] == "laser"
        assert decoy["device"] == {"kind": "lite", "name": "FSL-07"}
        assert decoy["dive"]["name"] == "Alligator Deep"
        assert decoy["fish_model"] == {"name": "Ginny", "known_length_m": 0.3127}
        assert decoy["species"] is None
        assert decoy["photo_url"] is None
        assert decoy["length_m"] == pytest.approx(0.305)

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
        }
        assert body["device_points"] is None
        assert body["algorithm"] == "laser_depth_fronto_parallel"

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
