"""``/tenants/{slug}/devices``: the first route, end to end over HTTP.

Every request is authenticated in-app from its bearer token, provisions the
caller's user row on first sight, resolves membership in the tenant named by
the path, and only then touches tenant data -- under RLS as the app role.
"""

import re
import time
from collections.abc import AsyncIterator
from typing import get_args

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.app import DeviceKind, create_app
from fishsense_services_api.auth import (
    KeysUnavailable,
    StaticKeySource,
    TokenValidator,
)

ISSUER = "https://auth.example.test/application/o/fishsense/"
AUDIENCE = "fishsense-web"
KID = "k1"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ALICE = "sub-alice"
BOB = "sub-bob"


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


def _validator(keys=None) -> TokenValidator:
    return TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=keys or StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )


_OFFLINE_ENGINE = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none")


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    )


@pytest.fixture
async def offline_client() -> AsyncIterator[httpx.AsyncClient]:
    """For requests refused before any query: its engine never connects."""
    app = create_app(engine=_OFFLINE_ENGINE, validator=_validator())
    async with await _client(app) as c:
        yield c


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    async with await _client(
        create_app(engine=app_engine, validator=_validator())
    ) as c:
        yield c


# --- authentication ---------------------------------------------------------


async def test_no_token_is_401_with_a_bearer_challenge(offline_client):
    response = await offline_client.get("/tenants/lab/devices")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_bad_token_is_401(offline_client):
    response = await offline_client.get(
        "/tenants/lab/devices", headers={"Authorization": "Bearer not.a.token"}
    )

    assert response.status_code == 401


async def test_an_unreachable_identity_provider_is_503_not_401():
    class Down:
        def key_for(self, kid):
            raise KeysUnavailable("jwks unreachable")

    app = create_app(engine=_OFFLINE_ENGINE, validator=_validator(keys=Down()))
    async with await _client(app) as client:
        response = await client.get("/tenants/lab/devices", headers=_bearer(ALICE))

    assert response.status_code == 503


# --- provisioning and membership ---------------------------------------------


async def test_the_first_request_provisions_the_caller(client, owner_engine):
    await client.get("/tenants/lab/devices", headers=_bearer("sub-newcomer"))

    async with owner_engine.connect() as conn:
        subs = (await conn.execute(text("SELECT sub FROM users"))).scalars().all()
    assert subs == ["sub-newcomer"]


async def test_a_non_member_and_an_unknown_tenant_look_the_same(
    client, seed_memberships
):
    await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})

    not_mine = await client.get("/tenants/partner/devices", headers=_bearer(ALICE))
    no_such = await client.get("/tenants/no-such/devices", headers=_bearer(ALICE))

    assert not_mine.status_code == no_such.status_code == 404
    assert not_mine.json() == no_such.json()


# --- tenant data ---------------------------------------------------------------


async def test_a_member_creates_and_lists_devices_in_their_tenant(
    client, seed_memberships
):
    await seed_memberships({ALICE: {"lab": "member"}})

    created = await client.post(
        "/tenants/lab/devices",
        json={"kind": "lite", "serial": "TG6-001"},
        headers=_bearer(ALICE),
    )
    listed = await client.get("/tenants/lab/devices", headers=_bearer(ALICE))

    assert created.status_code == 201
    assert created.json()["serial"] == "TG6-001"
    assert [d["serial"] for d in listed.json()] == ["TG6-001"]


async def test_a_duplicate_serial_in_the_same_tenant_is_a_409(client, seed_memberships):
    await seed_memberships({ALICE: {"lab": "member"}})
    device = {"kind": "lite", "serial": "TG6-001"}

    first = await client.post(
        "/tenants/lab/devices", json=device, headers=_bearer(ALICE)
    )
    again = await client.post(
        "/tenants/lab/devices", json=device, headers=_bearer(ALICE)
    )
    listed = await client.get("/tenants/lab/devices", headers=_bearer(ALICE))

    assert (first.status_code, again.status_code) == (201, 409)
    assert [d["serial"] for d in listed.json()] == ["TG6-001"]


async def test_an_unknown_device_kind_is_a_422_not_a_500(client, seed_memberships):
    await seed_memberships({ALICE: {"lab": "member"}})

    response = await client.post(
        "/tenants/lab/devices",
        json={"kind": "camera", "serial": "X-1"},
        headers=_bearer(ALICE),
    )

    assert response.status_code == 422


async def test_the_api_accepts_exactly_the_kinds_the_database_allows(owner_engine):
    """One list in the API, one CHECK in the schema: they must not drift."""
    async with owner_engine.connect() as conn:
        definition = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'devices_kind_check'"
                )
            )
        ).scalar_one()

    assert set(re.findall(r"'(\w+)'", definition)) == set(get_args(DeviceKind))


async def test_a_member_sees_only_their_own_tenants_devices(client, seed_memberships):
    await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})
    for sub, slug, serial in [(ALICE, "lab", "LAB-1"), (BOB, "partner", "PARTNER-1")]:
        await client.post(
            f"/tenants/{slug}/devices",
            json={"kind": "lite", "serial": serial},
            headers=_bearer(sub),
        )

    alice_sees = await client.get("/tenants/lab/devices", headers=_bearer(ALICE))

    assert [d["serial"] for d in alice_sees.json()] == ["LAB-1"]


async def test_a_member_cannot_create_in_a_tenant_they_do_not_belong_to(
    client, seed_memberships, owner_engine
):
    await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})

    response = await client.post(
        "/tenants/partner/devices",
        json={"kind": "lite", "serial": "SMUGGLED"},
        headers=_bearer(ALICE),
    )

    assert response.status_code == 404
    async with owner_engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM devices"))).scalar()
    assert count == 0


# --- API contract --------------------------------------------------------------


async def test_every_operation_has_an_explicit_operation_id(offline_client):
    """PLAN.md §3: generated clients depend on clean, stable operation ids."""
    spec = (await offline_client.get("/openapi.json")).json()
    ids = [op["operationId"] for path in spec["paths"].values() for op in path.values()]

    assert sorted(ids) == ["create_device", "list_devices"]
