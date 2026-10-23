"""Members by name, for their tenant's admins (the owner, 2026-10-22).

Users were known by their Authentik ``sub`` alone, and a skills page of hashes
can't be used. The owner chose to keep each person's name and email from their
token: recorded as they sign in, and shown to their tenants' admins and to
nobody else.

* a token's ``name`` and ``email`` claims are recorded on the caller's own row,
  and only theirs; a token without them leaves what was recorded;
* ``GET /tenants/{slug}/members`` lists the tenant's members, with their names,
  emails, roles and review levels, to its admins: a member is refused and a
  non-member sees nothing; no other tenant's members are listed.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_observations_api import (
    AUDIENCE,
    ISSUER,
    KID,
    SIGNING_KEY,
    client,
)  # noqa: F401

from fishsense_services_api.db import principal_transaction, tenant_transaction
from fishsense_services_api.review_store import set_skill

ADMIN, MEMBER, OTHER = "sub-admin", "sub-member", "sub-other"


def _bearer(sub: str, **claims) -> dict[str, str]:
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "iat": now,
        "exp": now + 60,
        **claims,
    }
    token = jwt.encode(body, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships(
        {
            ADMIN: {"lab": "admin"},
            MEMBER: {"lab": "member"},
            OTHER: {"partner": "admin"},
        }
    )


async def _profile(owner_engine, sub):
    async with owner_engine.connect() as conn:
        return tuple(
            (
                await conn.execute(
                    text("SELECT name, email FROM users WHERE sub = :s"), {"s": sub}
                )
            ).one()
        )


async def _members(client, sub=ADMIN, slug="lab", **claims) -> httpx.Response:
    return await client.get(f"/tenants/{slug}/members", headers=_bearer(sub, **claims))


async def test_a_callers_name_and_email_are_recorded(client, tenants, owner_engine):
    await _members(client, MEMBER, name="Mia Member", email="mia@example.org")

    assert await _profile(owner_engine, MEMBER) == ("Mia Member", "mia@example.org")


async def test_a_new_name_replaces_the_old_and_a_missing_one_keeps_it(
    client, tenants, owner_engine
):
    await _members(client, MEMBER, name="Mia Member", email="mia@example.org")
    await _members(client, MEMBER, name="Mia M. Member")
    await _members(client, MEMBER, email="mia@new.example.org")
    await _members(client, MEMBER)

    assert await _profile(owner_engine, MEMBER) == (
        "Mia M. Member",
        "mia@new.example.org",
    )


async def test_a_caller_records_only_their_own_profile(
    tenants, owner_engine, app_engine
):
    async with principal_transaction(app_engine, MEMBER) as conn:
        await conn.execute(text("SELECT record_profile('Mallory', 'm@evil.test')"))

    assert await _profile(owner_engine, MEMBER) == ("Mallory", "m@evil.test")
    assert await _profile(owner_engine, ADMIN) == (None, None)
    with pytest.raises(DBAPIError):
        async with principal_transaction(app_engine, MEMBER) as conn:
            await conn.execute(
                text("UPDATE users SET name = 'x' WHERE sub = :s"), {"s": ADMIN}
            )


async def test_an_admin_lists_the_tenants_members_by_name(client, tenants, app_engine):
    await _members(client, MEMBER, name="Mia Member", email="mia@example.org")
    await _members(client, OTHER, slug="partner", name="Otto Other")

    response = await _members(client, ADMIN, name="Ada Admin", email="ada@example.org")

    assert response.status_code == 200, response.text
    found = {m["name"]: m for m in response.json()}
    assert set(found) == {"Ada Admin", "Mia Member"}
    assert found["Mia Member"]["email"] == "mia@example.org"
    assert found["Mia Member"]["role"] == "member"
    assert found["Ada Admin"]["role"] == "admin"
    assert found["Mia Member"]["skills"] == {
        "laser": "learning",
        "head_tail": "learning",
        "species": "learning",
    }


async def test_the_list_shows_each_members_current_levels(
    client, tenants, app_engine, owner_engine
):
    await _members(client, MEMBER, name="Mia Member")
    lab = tenants["lab"]
    async with owner_engine.connect() as conn:
        ids = dict((await conn.execute(text("SELECT sub, id FROM users"))).all())
    async with tenant_transaction(app_engine, lab) as conn:
        await set_skill(conn, lab, ids[MEMBER], "laser", "expert", set_by=ids[ADMIN])
        await set_skill(conn, lab, ids[MEMBER], "laser", "trusted", set_by=ids[ADMIN])

    found = {m["name"]: m for m in (await _members(client)).json()}

    assert found["Mia Member"]["skills"]["laser"] == "trusted"


async def test_a_member_cannot_list_them(client, tenants):
    assert (await _members(client, MEMBER)).status_code == 403


async def test_a_non_member_sees_nothing(client, tenants):
    assert (await _members(client, OTHER)).status_code == 404


async def test_the_function_lists_only_the_active_tenants_members(tenants, app_engine):
    """Outside a tenant's transaction it lists nobody."""
    async with principal_transaction(app_engine, ADMIN) as conn:
        assert (await conn.execute(text("SELECT * FROM tenant_members()"))).all() == []
