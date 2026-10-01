"""The API's database role sees only the active tenant's rows.

PLAN.md §4.1 / §9.10: app-layer scoping is the first line; Postgres RLS is the
backstop. These tests pin the backstop, so they deliberately issue unscoped
queries -- no WHERE tenant_id = ... -- and rely on the database alone.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction


async def _seed_tenant(owner_engine: AsyncEngine, slug: str, serial: str) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        tenant_id = (
            await conn.execute(
                text(
                    "INSERT INTO tenants (slug, name) VALUES (:slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO devices (tenant_id, kind, serial) "
                "VALUES (:tenant_id, 'lite', :serial)"
            ),
            {"tenant_id": tenant_id, "serial": serial},
        )
    return tenant_id


async def _visible_serials(conn) -> list[str]:
    rows = await conn.execute(text("SELECT serial FROM devices ORDER BY serial"))
    return list(rows.scalars())


async def test_a_tenant_sees_only_its_own_rows(owner_engine, app_engine):
    lab = await _seed_tenant(owner_engine, "lab", "TG6-LAB")
    await _seed_tenant(owner_engine, "partner", "TG6-PARTNER")

    async with tenant_transaction(app_engine, lab) as conn:
        assert await _visible_serials(conn) == ["TG6-LAB"]


async def test_no_active_tenant_sees_nothing(owner_engine, app_engine):
    await _seed_tenant(owner_engine, "lab", "TG6-LAB")

    async with app_engine.begin() as conn:
        assert await _visible_serials(conn) == []


async def test_cannot_write_a_row_into_another_tenant(owner_engine, app_engine):
    lab = await _seed_tenant(owner_engine, "lab", "TG6-LAB")
    partner = await _seed_tenant(owner_engine, "partner", "TG6-PARTNER")

    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_transaction(app_engine, lab) as conn:
            await conn.execute(
                text(
                    "INSERT INTO devices (tenant_id, kind, serial) "
                    "VALUES (:tenant_id, 'lite', 'SMUGGLED')"
                ),
                {"tenant_id": partner},
            )


async def test_active_tenant_does_not_leak_to_the_next_transaction(
    owner_engine, app_engine
):
    """A pooled connection must not carry the tenant into its next use."""
    lab = await _seed_tenant(owner_engine, "lab", "TG6-LAB")

    async with app_engine.connect() as conn:
        async with tenant_transaction(conn, lab) as scoped:
            assert await _visible_serials(scoped) == ["TG6-LAB"]
        async with conn.begin():
            assert await _visible_serials(conn) == []


async def test_app_role_cannot_bypass_row_level_security(owner_engine):
    async with owner_engine.connect() as conn:
        role = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = 'fishsense_app'"
                )
            )
        ).one()
        forced = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'devices'"
                )
            )
        ).scalar_one()
        owner = (
            await conn.execute(
                text("SELECT tableowner FROM pg_tables WHERE tablename = 'devices'")
            )
        ).scalar_one()

    assert role == (False, False)
    assert forced is True
    assert owner != "fishsense_app"
