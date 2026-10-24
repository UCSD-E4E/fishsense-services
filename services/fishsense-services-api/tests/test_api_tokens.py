"""Personal API tokens (the owner, 2026-10-23).

A signed-in user makes a token for a script or an agent; it acts as them. Two
kinds: **read** (the default) is refused every write request (a GET keeps its
own server-side effects, such as a system admin's visit record), and **edit**,
which they opt into,
may write what their role allows -- through the REST API only, as the MCP
endpoint takes read tokens alone. A token's kind is fixed when it is made.

No token, of either kind, may unlock a tenant or manage tokens, and an edit
token writes only where its owner is a member: a system admin's unlock is for
them, in the web app, not for their scripts.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from depth_measure_seed import exec_
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from fishsense_services_api.api_tokens import TOKEN_PREFIX
from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator

ISSUER, AUDIENCE, KID = "https://idp.test", "fishsense-api", "test-key"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OWNER, OTHER, OPERATOR = "owner-sub", "other-sub", "operator-sub"


def _signed_in(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


def _token(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
async def tenants(seed_memberships, owner_engine):
    found = await seed_memberships(
        {
            OWNER: {"lab": "admin"},
            OTHER: {"partner": "admin"},
            OPERATOR: {"lab": "member"},
        }
    )
    await exec_(
        owner_engine,
        "UPDATE users SET is_system_admin = true WHERE sub = :s",
        s=OPERATOR,
    )
    return found


def _app(app_engine):
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: KEY.public_key()}),
    )
    return create_app(engine=app_engine, validator=validator)


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(app_engine)),
        base_url="http://api.test",
    ) as c:
        yield c


async def _make(client, sub=OWNER, **body) -> dict:
    response = await client.post(
        "/me/tokens", headers=_signed_in(sub), json={"name": "agent", **body}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── Making, listing and revoking ─────────────────────────────────────────────────────


async def test_a_token_is_read_only_unless_asked(client, tenants):
    made = await _make(client)

    assert made["scope"] == "read"
    assert made["token"].startswith(TOKEN_PREFIX)


async def test_an_edit_token_is_an_opt_in(client, tenants):
    assert (await _make(client, scope="edit"))["scope"] == "edit"


async def test_the_secret_is_shown_once_and_only_its_hash_kept(
    client, tenants, owner_engine
):
    made = await _make(client)

    listed = (await client.get("/me/tokens", headers=_signed_in(OWNER))).json()
    assert [t["id"] for t in listed] == [made["id"]]
    assert "token" not in listed[0]
    assert made["token"].startswith(listed[0]["prefix"])
    async with owner_engine.connect() as conn:
        stored = (await conn.execute(text("SELECT token_hash FROM api_tokens"))).one()
    assert stored.token_hash == hashlib.sha256(made["token"].encode()).hexdigest()


async def test_every_token_expires_by_default_in_90_days(client, tenants):
    made = await _make(client)

    left = datetime.fromisoformat(made["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(days=89) < left <= timedelta(days=90)


@pytest.mark.parametrize("days", [0, 366])
async def test_a_token_lasts_a_day_to_a_year(client, tenants, days):
    response = await client.post(
        "/me/tokens",
        headers=_signed_in(OWNER),
        json={"name": "agent", "expires_in_days": days},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
async def test_a_token_needs_a_name(client, tenants, name):
    response = await client.post(
        "/me/tokens", headers=_signed_in(OWNER), json={"name": name}
    )

    assert response.status_code == 422


async def test_a_token_kind_is_read_or_edit(client, tenants):
    response = await client.post(
        "/me/tokens", headers=_signed_in(OWNER), json={"name": "a", "scope": "admin"}
    )

    assert response.status_code == 422


async def test_tokens_are_per_user(client, tenants):
    mine = await _make(client)

    theirs = (await client.get("/me/tokens", headers=_signed_in(OTHER))).json()
    revoked = await client.delete(f"/me/tokens/{mine['id']}", headers=_signed_in(OTHER))

    assert theirs == []
    assert revoked.status_code == 404


async def test_a_revoked_token_stops_working(client, tenants):
    made = await _make(client)

    revoked = await client.delete(f"/me/tokens/{made['id']}", headers=_signed_in(OWNER))

    assert revoked.status_code == 204
    assert (await client.get("/me", headers=_token(made["token"]))).status_code == 401
    listed = (await client.get("/me/tokens", headers=_signed_in(OWNER))).json()
    assert listed[0]["revoked_at"] is not None


async def test_an_expired_token_stops_working(client, tenants, owner_engine):
    made = await _make(client)
    await exec_(
        owner_engine,
        "UPDATE api_tokens SET expires_at = now() - interval '1 second'",
    )

    assert (await client.get("/me", headers=_token(made["token"]))).status_code == 401


@pytest.mark.parametrize("raw", [TOKEN_PREFIX + "nope", "nope"])
async def test_an_unknown_token_is_refused(client, tenants, raw):
    assert (await client.get("/me", headers=_token(raw))).status_code == 401


async def test_use_is_recorded(client, tenants):
    made = await _make(client)

    await client.get("/me", headers=_token(made["token"]))

    listed = (await client.get("/me/tokens", headers=_signed_in(OWNER))).json()
    assert listed[0]["last_used_at"] is not None


async def test_a_user_holds_at_most_25_live_tokens(client, tenants):
    for _ in range(25):
        await _make(client)

    response = await client.post(
        "/me/tokens", headers=_signed_in(OWNER), json={"name": "one too many"}
    )

    assert response.status_code == 409


# ── What a token may do ──────────────────────────────────────────────────────────────


async def test_a_token_acts_as_its_owner(client, tenants):
    made = await _make(client)

    response = await client.get("/tenants/lab/summary", headers=_token(made["token"]))
    elsewhere = await client.get(
        "/tenants/partner/summary", headers=_token(made["token"])
    )

    assert response.status_code == 200
    assert elsewhere.status_code == 404


async def test_a_read_token_is_refused_a_write(client, tenants):
    made = await _make(client)

    response = await client.post(
        "/tenants/lab/deployments", headers=_token(made["token"]), json={"name": "t"}
    )

    assert response.status_code == 403
    assert "read-only token" in response.json()["detail"]


def test_every_write_route_refuses_a_read_token(app_engine):
    """The rule lives in one place, `authenticate`: every route that names a
    caller goes through it, so a new write route can't skip it."""
    app = _app(app_engine)

    def calls(dependant) -> set[str]:
        found = set()
        for d in dependant.dependencies:
            found.add(getattr(d.call, "__name__", ""))
            found |= calls(d)
        return found

    unauthenticated = sorted(
        r.path
        for r in app.routes
        if hasattr(r, "dependant") and "authenticate" not in calls(r.dependant)
    )
    assert unauthenticated == ["/healthz", "/public/summary"]


async def test_a_read_token_is_refused_on_every_write_route(
    client, tenants, app_engine
):
    made = await _make(client)
    app = _app(app_engine)
    writes = [
        (method, r.path)
        for r in app.routes
        if hasattr(r, "dependant")
        for method in sorted(getattr(r, "methods", set()) - {"GET", "HEAD"})
    ]
    assert writes, "no write routes found"

    refused = {}
    for method, path in writes:
        url = (
            path.replace("{slug}", "lab")
            .replace("{number}", "1")
            .replace("{source_number}", "2")
            .replace("{target_number}", "2")
            .replace("{deployment_number}", "1")
            .replace("{kind}", "laser")
            .replace("{user_id}", "00000000-0000-0000-0000-000000000000")
            .replace("{capture_id}", "00000000-0000-0000-0000-000000000000")
            .replace("{client_capture_uuid}", "00000000-0000-0000-0000-000000000000")
            .replace("{token_id}", "00000000-0000-0000-0000-000000000000")
        )
        response = await client.request(
            method, url, headers=_token(made["token"]), json={}
        )
        refused[(method, path)] = response.status_code

    assert {k: v for k, v in refused.items() if v != 403} == {}


async def test_an_edit_token_writes_where_its_owner_may(client, tenants):
    made = await _make(client, scope="edit")

    response = await client.post(
        "/tenants/lab/deployments", headers=_token(made["token"]), json={"name": "t"}
    )

    assert response.status_code == 201


async def test_an_edit_token_keeps_its_owners_role(client, tenants):
    made = await _make(client, sub=OPERATOR, scope="edit")

    # A member, not an admin: the admin-only edit is refused as for them.
    response = await client.put(
        "/tenants/lab/members/00000000-0000-0000-0000-000000000000/skills/laser",
        headers=_token(made["token"]),
        json={"level": "trusted"},
    )

    assert response.status_code == 403
    assert "token" not in response.json()["detail"]


async def test_a_system_admins_token_reads_any_tenant_and_is_recorded(
    client, tenants, owner_engine
):
    made = await _make(client, sub=OPERATOR)

    response = await client.get(
        "/tenants/partner/summary", headers=_token(made["token"])
    )

    assert response.status_code == 200
    async with owner_engine.connect() as conn:
        actions = (
            await conn.execute(text("SELECT action FROM system_admin_access"))
        ).scalars()
        assert list(actions) == ["view"]


async def test_no_token_may_unlock_a_tenant(client, tenants):
    made = await _make(client, sub=OPERATOR, scope="edit")

    response = await client.post(
        "/tenants/partner/system-admin/unlock", headers=_token(made["token"])
    )

    assert response.status_code == 403


async def test_an_edit_token_writes_nothing_where_its_owner_only_unlocked(
    client, tenants
):
    made = await _make(client, sub=OPERATOR, scope="edit")
    unlocked = await client.post(
        "/tenants/partner/system-admin/unlock", headers=_signed_in(OPERATOR)
    )
    assert unlocked.status_code == 200

    write = await client.post(
        "/tenants/partner/deployments",
        headers=_token(made["token"]),
        json={"name": "t"},
    )
    read = await client.get("/tenants/partner/summary", headers=_token(made["token"]))

    assert write.status_code == 403
    assert read.status_code == 200


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/me/tokens"),
        ("POST", "/me/tokens"),
        ("DELETE", "/me/tokens/00000000-0000-0000-0000-000000000000"),
    ],
)
async def test_no_token_may_manage_tokens(client, tenants, method, path):
    made = await _make(client, scope="edit")

    response = await client.request(
        method, path, headers=_token(made["token"]), json={"name": "x"}
    )

    assert response.status_code == 403


# ── The table itself ─────────────────────────────────────────────────────────────────


async def test_a_token_kind_cannot_change(client, tenants, owner_engine):
    await _make(client)

    with pytest.raises(DBAPIError, match="scope"):
        await exec_(owner_engine, "UPDATE api_tokens SET scope = 'edit'")


async def test_the_app_role_cannot_read_the_table(app_engine, tenants):
    """Only the functions, running as the owner, touch it."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT count(*) FROM api_tokens"))


async def test_the_cap_holds_under_concurrent_makes(client, tenants):
    """Each make counts the caller's live tokens and inserts; without a lock on
    the caller, concurrent makes all see room and overshoot."""
    import asyncio

    for _ in range(20):
        await _make(client)

    responses = await asyncio.gather(
        *(
            client.post(
                "/me/tokens", headers=_signed_in(OWNER), json={"name": f"burst {i}"}
            )
            for i in range(10)
        )
    )

    assert sorted(r.status_code for r in responses) == [201] * 5 + [409] * 5
    listed = (await client.get("/me/tokens", headers=_signed_in(OWNER))).json()
    assert len(listed) == 25


def test_the_lifetime_limit_is_one_number():
    """The API and the database refuse the same lifetimes."""
    import importlib

    from fishsense_services_api import api_tokens

    migration = importlib.import_module(
        "fishsense_services_api.migrations.versions.0066_api_tokens"
    )
    assert migration.MAX_DAYS == api_tokens.MAX_DAYS
    assert migration.MAX_LIVE == api_tokens.MAX_LIVE


async def test_a_lifetime_the_database_refuses_is_an_error_not_the_cap(
    app_engine, tenants
):
    """Only the cap is a quiet "no": anything else the API should have
    stopped is loud, never reported as too many tokens."""
    from fishsense_services_api.api_tokens import create_token
    from fishsense_services_api.db import principal_transaction

    with pytest.raises(DBAPIError, match="lifetime"):
        async with principal_transaction(app_engine, OWNER) as conn:
            await create_token(conn, name="a", scope="read", days=400)
