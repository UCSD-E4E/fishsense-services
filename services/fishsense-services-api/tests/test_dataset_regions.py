"""The whole dataset's map, for system admins (the owner, 2026-11-11): every
tenant's fish by broad region, opted in to the public page or not.

The rules are each tenant's Today map's (`tenant_regions_store`), added up:
read a tenant at a time, in that tenant's transaction, so RLS still scopes
every read. A tenant the system admin is not a member of is opened the way
its pages are (0056), so the visit is recorded. Anyone else finds the page
not there (404), as with every tenant list.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import AsyncIterator

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from depth_measure_seed import exec_, forget_identities  # noqa: F401
from observation_seed import lite
from sqlalchemy import text

from fishsense_services_api.app import create_app
from fishsense_services_api.regions import REGIONS
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.tenant_regions_store import (
    TenantRegion,
    TenantRegions,
    combine,
)

ISSUER, AUDIENCE, KID = "https://idp.test", "fishsense-api", "test-key"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OPERATOR, MEMBER = "operator-sub", "member-sub"
KEYS = "Florida Keys, USA"
SOCAL = "Southern California, USA"
_devices = itertools.count(10)


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def tenants(seed_memberships, owner_engine):
    """The operator is in the lab only; the partner has not opted in to the
    public page (``public_totals`` stays false)."""
    found = await seed_memberships(
        {OPERATOR: {"lab": "member"}, MEMBER: {"partner": "admin"}}
    )
    await exec_(
        owner_engine,
        "UPDATE users SET is_system_admin = true WHERE sub = :s",
        s=OPERATOR,
    )
    return found


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: KEY.public_key()}),
    )
    app = create_app(engine=app_engine, validator=validator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


async def _get(client, path, sub=OPERATOR):
    return await client.get(path, headers=_bearer(sub))


async def _fish(owner_engine, tenant_id, folder: str | None) -> None:
    """A measured wild Lite fish whose mission sits in ``folder``."""
    capture_id, _ = await lite(
        owner_engine,
        tenant_id,
        species=("A b", "C"),
        device_name=f"FSL-{next(_devices)}",
    )
    if folder is not None:
        await exec_(
            owner_engine,
            "UPDATE missions SET source_path = :p WHERE id = "
            "(SELECT mission_id FROM captures WHERE id = :c)",
            p=f"/nas/{folder}/FSL-07 dive",
            c=capture_id,
        )


async def _access(owner_engine) -> list[tuple[str, str]]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT t.slug, a.action FROM system_admin_access a
            JOIN tenants t ON t.id = a.tenant_id ORDER BY a.at, a.action
            """))
        return [tuple(r) for r in rows]


class TestCombine:
    def test_regions_add_up_by_name_most_first(self):
        keys, socal = REGIONS[KEYS], REGIONS[SOCAL]

        found = combine(
            [
                TenantRegions(
                    regions=[
                        TenantRegion(SOCAL, *socal, 3),
                        TenantRegion(KEYS, *keys, 2),
                    ],
                    elsewhere=1,
                ),
                TenantRegions(regions=[TenantRegion(KEYS, *keys, 2)], elsewhere=2),
            ]
        )

        assert found == TenantRegions(
            regions=[TenantRegion(KEYS, *keys, 4), TenantRegion(SOCAL, *socal, 3)],
            elsewhere=3,
        )

    def test_nothing_adds_up_to_an_empty_map(self):
        assert combine([]) == TenantRegions(regions=[], elsewhere=0)


async def test_a_system_admin_sees_every_tenants_fish(client, tenants, owner_engine):
    await _fish(owner_engine, tenants["lab"], "102724_Islamorada")
    await _fish(owner_engine, tenants["partner"], "Alligator Reef")
    await _fish(owner_engine, tenants["partner"], "San Diego")
    await _fish(owner_engine, tenants["partner"], None)

    response = await _get(client, "/admin/regions")

    assert response.status_code == 200
    found = response.json()
    assert [(r["name"], r["fish"]) for r in found["regions"]] == [
        (KEYS, 2),
        (SOCAL, 1),
    ]
    assert found["elsewhere"] == 1


async def test_anyone_else_finds_it_not_there(client, tenants):
    response = await _get(client, "/admin/regions", sub=MEMBER)

    assert response.status_code == 404


async def test_reading_a_tenant_they_are_not_in_is_recorded(
    client, tenants, owner_engine
):
    await _get(client, "/admin/regions")

    assert await _access(owner_engine) == [("partner", "view")]


async def test_the_automatic_lengths_are_counted_on_request(
    client, tenants, owner_engine
):
    """A human length is not an automatic one: the toggle asks the other rules."""
    await _fish(owner_engine, tenants["partner"], "Alligator Reef")

    response = await _get(client, "/admin/regions?source=automatic")

    assert response.status_code == 200
    assert response.json() == {"regions": [], "elsewhere": 0}


async def test_an_unknown_source_is_refused(client, tenants):
    response = await _get(client, "/admin/regions?source=everything")

    assert response.status_code == 422
