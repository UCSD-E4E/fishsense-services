"""Resolving the caller's membership in the tenant named by the URL.

PLAN.md §9.10: the active tenant comes from the path, and the API checks
membership on every request, keyed on the IdP's stable ``sub``. Resolution
runs before any tenant is active, so it is isolated per *caller*: nobody can
read another user's memberships, or even learn which tenants exist.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from fishsense_services_api.db import principal_transaction
from fishsense_services_api.memberships import Membership, resolve_membership

ALICE = "sub-alice"
BOB = "sub-bob"


async def _column(conn, sql: str) -> list:
    return sorted((await conn.execute(text(sql))).scalars())


async def test_a_member_resolves_to_the_tenant_and_role(seed_memberships, app_engine):
    ids = await seed_memberships({ALICE: {"lab": "admin", "partner": "member"}})

    async with principal_transaction(app_engine, ALICE) as conn:
        assert await resolve_membership(conn, ALICE, "partner") == Membership(
            tenant_id=ids["partner"], role="member"
        )


async def test_a_non_member_does_not_resolve(seed_memberships, app_engine):
    await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})

    async with principal_transaction(app_engine, ALICE) as conn:
        assert await resolve_membership(conn, ALICE, "partner") is None


async def test_an_unknown_tenant_does_not_resolve(seed_memberships, app_engine):
    await seed_memberships({ALICE: {"lab": "admin"}})

    async with principal_transaction(app_engine, ALICE) as conn:
        assert await resolve_membership(conn, ALICE, "no-such-tenant") is None


async def test_a_caller_sees_only_their_own_user_and_memberships(
    seed_memberships, app_engine
):
    await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})

    async with principal_transaction(app_engine, ALICE) as conn:
        assert await _column(conn, "SELECT sub FROM users") == [ALICE]
        assert await _column(conn, "SELECT role FROM memberships") == ["admin"]


async def test_a_caller_cannot_discover_tenants_they_do_not_belong_to(
    seed_memberships, app_engine
):
    await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})

    async with principal_transaction(app_engine, ALICE) as conn:
        assert await _column(conn, "SELECT slug FROM tenants") == ["lab"]


async def test_no_caller_sees_nothing(seed_memberships, app_engine):
    await seed_memberships({ALICE: {"lab": "admin"}})

    async with app_engine.begin() as conn:
        assert await _column(conn, "SELECT sub FROM users") == []
        assert await _column(conn, "SELECT role FROM memberships") == []
        assert await _column(conn, "SELECT slug FROM tenants") == []


async def test_the_caller_does_not_leak_to_the_next_transaction(
    seed_memberships, app_engine
):
    await seed_memberships({ALICE: {"lab": "admin"}})

    async with app_engine.connect() as conn:
        async with principal_transaction(conn, ALICE) as scoped:
            assert await _column(scoped, "SELECT sub FROM users") == [ALICE]
        async with conn.begin():
            assert await _column(conn, "SELECT sub FROM users") == []


async def test_the_app_role_cannot_grant_itself_membership(
    seed_memberships, app_engine
):
    ids = await seed_memberships({ALICE: {"lab": "admin"}, BOB: {"partner": "admin"}})

    async with principal_transaction(app_engine, ALICE) as conn:
        alice_id = (await conn.execute(text("SELECT id FROM users"))).scalar_one()

    with pytest.raises(DBAPIError, match="permission denied"):
        async with principal_transaction(app_engine, ALICE) as conn:
            await conn.execute(
                text(
                    "INSERT INTO memberships (tenant_id, user_id, role) "
                    "VALUES (:t, :u, 'admin')"
                ),
                {"t": ids["partner"], "u": alice_id},
            )
