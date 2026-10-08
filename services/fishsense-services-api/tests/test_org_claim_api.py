"""Over HTTP: a partner's first request lands them in their org's tenant.

The membership step of every tenant route (app.py) now joins the caller to the
tenant that claims their ``org`` before resolving, so a partner who has just
enrolled through their org's invite is a ``member`` on their first call --
and still a stranger (404) everywhere else. ``GET /me/memberships`` tells the
web which tenants the caller is in, since a partner's isn't the lab.
"""

import time
from collections.abc import AsyncIterator

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator

ISSUER = "https://auth.example.test/application/o/fishsense/"
AUDIENCE = "fishsense-web"
KID = "k1"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
LAB_ADMIN = "sub-lab-admin"
PARTNER = "sub-partner"
ORG = "conservation-angler"


def _bearer(sub: str, org: str | None = None) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60,
              "org": org}  # fmt: skip
    token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )
    app = create_app(engine=app_engine, validator=validator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


@pytest.fixture
async def tenants(seed_memberships, owner_engine) -> dict:
    ids = await seed_memberships({LAB_ADMIN: {"lab": "admin", ORG: "admin"}})
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenants SET org_claim = :org, name = 'Conservation Angler' "
                "WHERE slug = :org"
            ),
            {"org": ORG},
        )
    return ids


async def test_a_partners_first_request_finds_them_a_member(tenants, client):
    response = await client.get(
        f"/tenants/{ORG}/membership", headers=_bearer(PARTNER, ORG)
    )

    assert response.status_code == 200
    assert response.json() == {"role": "member", "is_admin": False}


async def test_a_partner_is_still_a_stranger_to_the_lab(tenants, client):
    response = await client.get(
        "/tenants/lab/membership", headers=_bearer(PARTNER, ORG)
    )

    assert response.status_code == 404


async def test_without_the_org_claim_there_is_no_way_in(tenants, client):
    response = await client.get(f"/tenants/{ORG}/membership", headers=_bearer(PARTNER))

    assert response.status_code == 404


async def test_a_partner_is_not_an_admin_of_their_tenant(tenants, client):
    response = await client.put(
        f"/tenants/{ORG}/dives/1/calibration-source/2", headers=_bearer(PARTNER, ORG)
    )

    assert response.status_code == 403


async def test_my_memberships_lists_a_partners_tenant(tenants, client):
    response = await client.get("/me/memberships", headers=_bearer(PARTNER, ORG))

    assert response.status_code == 200
    assert response.json() == [
        {
            "slug": ORG,
            "name": "Conservation Angler",
            "role": "member",
            "is_admin": False,
        }
    ]


async def test_my_memberships_lists_only_the_callers_own(tenants, client):
    response = await client.get("/me/memberships", headers=_bearer(LAB_ADMIN))

    assert [(m["slug"], m["role"]) for m in response.json()] == [
        (ORG, "admin"),
        ("lab", "admin"),
    ]


async def test_a_stranger_has_no_memberships(tenants, client):
    response = await client.get("/me/memberships", headers=_bearer("sub-nobody"))

    assert response.status_code == 200
    assert response.json() == []


async def test_my_memberships_needs_a_token(tenants, client):
    assert (await client.get("/me/memberships")).status_code == 401
