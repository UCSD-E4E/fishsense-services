"""System admins (docs/design/README.md, Tenants; the owner, 2026-10-20).

A system admin -- a flag an operator sets with ``system-admin`` -- may open any
tenant. In one they are not a member of they arrive **read-only**: they see its
pages, a banner says why they can, and the visit is recorded. Writing takes a
deliberate, time-limited, recorded **unlock**, after which they act as its
admin. Everyone else sees a tenant they are not in as not there (404).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from depth_measure_seed import exec_, forget_identities  # noqa: F401
from sqlalchemy import text

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator

ISSUER, AUDIENCE, KID = "https://idp.test", "fishsense-api", "test-key"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OPERATOR, MEMBER = "operator-sub", "member-sub"


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def tenants(seed_memberships, owner_engine):
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


async def _post(client, path, body=None, sub=OPERATOR):
    return await client.post(path, headers=_bearer(sub), json=body or {})


async def _access(owner_engine) -> list[tuple[str, str]]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT t.slug, a.action FROM system_admin_access a
            JOIN tenants t ON t.id = a.tenant_id ORDER BY a.at, a.action
            """))
        return [tuple(r) for r in rows]


async def test_a_system_admin_reads_a_tenant_they_are_not_in(client, tenants):
    response = await _get(client, "/tenants/partner/summary")

    assert response.status_code == 200


async def test_anyone_else_finds_it_not_there(client, tenants):
    response = await _get(client, "/tenants/lab/summary", sub=MEMBER)

    assert response.status_code == 404


async def test_the_visit_is_recorded_once_an_hour(client, tenants, owner_engine):
    await _get(client, "/tenants/partner/summary")
    await _get(client, "/tenants/partner/deployments")

    assert await _access(owner_engine) == [("partner", "view")]


async def test_their_own_tenant_is_theirs_as_usual_and_not_recorded(
    client, tenants, owner_engine
):
    assert (await _get(client, "/tenants/lab/summary")).status_code == 200
    assert await _access(owner_engine) == []


async def test_they_arrive_read_only(client, tenants):
    response = await _post(client, "/tenants/partner/deployments", {"name": "trip"})

    assert response.status_code == 403
    assert "unlock" in response.json()["detail"]


async def test_the_membership_says_how_they_are_there(client, tenants):
    read_only = (await _get(client, "/tenants/partner/membership")).json()
    own = (await _get(client, "/tenants/lab/membership")).json()

    assert read_only["access"] == "system_admin_read_only"
    assert read_only["is_admin"] is False
    assert own["access"] == "member"


async def test_an_unlock_makes_them_its_admin_for_a_while_and_is_recorded(
    client, tenants, owner_engine
):
    unlocked = await _post(client, "/tenants/partner/system-admin/unlock")

    assert unlocked.status_code == 200
    until = datetime.fromisoformat(unlocked.json()["until"])
    assert (
        timedelta(minutes=55)
        < until - datetime.now(timezone.utc)
        < timedelta(minutes=65)
    )
    created = await _post(client, "/tenants/partner/deployments", {"name": "trip"})
    assert created.status_code == 201
    membership = (await _get(client, "/tenants/partner/membership")).json()
    assert (membership["access"], membership["is_admin"]) == (
        "system_admin_unlocked",
        True,
    )
    assert ("partner", "unlock") in await _access(owner_engine)


async def test_an_unlock_runs_out(client, tenants, owner_engine):
    await _post(client, "/tenants/partner/system-admin/unlock")
    await exec_(
        owner_engine,
        "UPDATE system_admin_access SET until = now() - interval '1 minute'",
    )

    response = await _post(client, "/tenants/partner/deployments", {"name": "trip"})

    assert response.status_code == 403


async def test_only_a_system_admin_may_unlock(client, tenants):
    response = await _post(client, "/tenants/lab/system-admin/unlock", sub=MEMBER)

    assert response.status_code == 404


async def test_a_system_admin_lists_every_tenant(client, tenants):
    everyone = await _get(client, "/admin/tenants")
    nobody = await _get(client, "/admin/tenants", sub=MEMBER)

    assert sorted(t["slug"] for t in everyone.json()) == ["lab", "partner"]
    assert nobody.status_code == 404


async def test_the_caller_learns_whether_they_are_one(client, tenants):
    assert (await _get(client, "/me")).json()["is_system_admin"] is True
    assert (await _get(client, "/me", sub=MEMBER)).json()["is_system_admin"] is False


async def test_a_tenant_they_belong_to_is_not_theirs_to_unlock(
    client, tenants, owner_engine
):
    """Their own tenant gives them their own role: an unlock would be a way
    round it, so there is none."""
    assert (await _post(client, "/tenants/lab/system-admin/unlock")).status_code == 404
    assert await _access(owner_engine) == []


def test_every_tenant_write_goes_through_the_read_only_rule(app_engine):
    """The rule lives in one place, the membership dependency: a write route
    that skipped it would let a read-only system admin write. Only the unlock
    may, being the way out of read-only."""
    validator = TokenValidator(
        issuer=ISSUER, audiences=(AUDIENCE,), keys=StaticKeySource({})
    )
    app = create_app(engine=app_engine, validator=validator)

    def calls(dependant) -> set[str]:
        found = set()
        for d in dependant.dependencies:
            found.add(getattr(d.call, "__name__", ""))
            found |= calls(d)
        return found

    bypass = sorted(
        r.path
        for r in app.routes
        if "{slug}" in getattr(r, "path", "")
        and getattr(r, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
        and "membership" not in calls(r.dependant)
    )
    assert bypass == ["/tenants/{slug}/system-admin/unlock"]


async def test_the_app_role_cannot_read_the_record_itself(app_engine, tenants):
    """Only the functions, running as the owner, touch it."""
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError, match="permission denied"):
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT count(*) FROM system_admin_access"))
