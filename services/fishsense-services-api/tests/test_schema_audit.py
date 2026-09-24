"""A schema-wide tenancy audit: no table can ship without isolation.

Every table in ``public`` must be classified. Tenant-scoped tables (the default)
need a non-null ``tenant_id`` referencing ``tenants``, RLS enabled *and* forced,
and a policy keyed on the active tenant. Caller-scoped tables (users,
memberships, tenants) have their own policies. Global reference tables are
read-only to the app role. And the app role owns nothing.

Each violation case creates a rogue table inside a transaction that is rolled
back, so the audit is shown to catch it without leaving anything behind.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.schema_audit import (
    GLOBAL_REFERENCE_TABLES,
    tenancy_violations,
)

APP_ROLE = "fishsense_app"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


@pytest.fixture
async def scratch(owner_engine) -> AsyncIterator[AsyncConnection]:
    """An owner connection whose changes are always rolled back."""
    async with owner_engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()


async def _audit(conn: AsyncConnection, **options) -> list[str]:
    return await tenancy_violations(conn, app_role=APP_ROLE, **options)


async def test_the_migrated_schema_has_no_violations(scratch):
    assert await _audit(scratch) == []


async def test_an_unclassified_table_without_tenant_id_is_flagged(scratch):
    await scratch.execute(text("CREATE TABLE rogue (id int PRIMARY KEY)"))

    assert any("rogue" in v and "tenant_id" in v for v in await _audit(scratch))


async def test_a_tenant_table_whose_rls_is_not_forced_is_flagged(scratch):
    await scratch.execute(text("""
            CREATE TABLE lax (
                id int PRIMARY KEY,
                tenant_id uuid NOT NULL REFERENCES tenants (id)
            )
            """))
    await scratch.execute(text("ALTER TABLE lax ENABLE ROW LEVEL SECURITY"))
    await scratch.execute(text(f"""
            CREATE POLICY tenant_isolation ON lax
                USING (tenant_id = {ACTIVE_TENANT})
                WITH CHECK (tenant_id = {ACTIVE_TENANT})
            """))

    assert any("lax" in v and "forced" in v for v in await _audit(scratch))


async def test_a_tenant_table_without_a_tenant_policy_is_flagged(scratch):
    await scratch.execute(text("""
            CREATE TABLE unguarded (
                id int PRIMARY KEY,
                tenant_id uuid NOT NULL REFERENCES tenants (id)
            )
            """))
    await scratch.execute(text("ALTER TABLE unguarded ENABLE ROW LEVEL SECURITY"))
    await scratch.execute(text("ALTER TABLE unguarded FORCE ROW LEVEL SECURITY"))

    assert any("unguarded" in v and "policy" in v for v in await _audit(scratch))


async def test_an_extra_permissive_policy_on_a_tenant_table_is_flagged(scratch):
    """Permissive policies are OR-ed: one ``USING (true)`` opens every tenant."""
    await scratch.execute(
        text("CREATE POLICY read_all ON dives FOR SELECT USING (true)")
    )

    assert any("dives" in v and "read_all" in v for v in await _audit(scratch))


async def test_a_loosened_tenant_policy_is_flagged(scratch):
    """Mentioning app.tenant_id isn't enough; the expression must be exact."""
    await scratch.execute(text("DROP POLICY tenant_isolation ON dives"))
    await scratch.execute(text(f"""
            CREATE POLICY tenant_isolation ON dives
                USING (tenant_id = {ACTIVE_TENANT} OR true)
                WITH CHECK (tenant_id = {ACTIVE_TENANT})
            """))

    assert any("dives" in v for v in await _audit(scratch))


async def test_a_tenant_policy_that_skips_some_roles_is_flagged(scratch):
    await scratch.execute(text("DROP POLICY tenant_isolation ON dives"))
    await scratch.execute(text(f"""
            CREATE POLICY tenant_isolation ON dives TO {APP_ROLE}
                USING (tenant_id = {ACTIVE_TENANT})
                WITH CHECK (tenant_id = {ACTIVE_TENANT})
            """))

    assert any("dives" in v for v in await _audit(scratch))


async def test_an_extra_permissive_policy_on_a_caller_table_is_flagged(scratch):
    await scratch.execute(
        text("CREATE POLICY everyone ON users FOR SELECT USING (true)")
    )

    assert any("users" in v and "everyone" in v for v in await _audit(scratch))


async def test_a_restrictive_policy_can_only_narrow_so_it_is_fine(scratch):
    await scratch.execute(
        text(
            "CREATE POLICY no_parked ON dives AS RESTRICTIVE FOR SELECT "
            "USING (priority <> 'none')"
        )
    )

    assert await _audit(scratch) == []


async def test_a_nullable_or_unreferenced_tenant_id_is_flagged(scratch):
    await scratch.execute(
        text("CREATE TABLE loose (id int PRIMARY KEY, tenant_id uuid)")
    )

    assert any("loose" in v and "tenant_id" in v for v in await _audit(scratch))


async def test_a_global_table_the_app_role_can_write_is_flagged(scratch):
    await scratch.execute(text("CREATE TABLE lookup (id int PRIMARY KEY)"))
    await scratch.execute(text(f"GRANT SELECT, INSERT ON lookup TO {APP_ROLE}"))

    violations = await _audit(scratch, global_tables={"lookup"})

    assert any("lookup" in v and "write" in v for v in violations)


async def test_a_global_table_the_app_role_can_only_read_is_fine(scratch):
    await scratch.execute(text("CREATE TABLE lookup (id int PRIMARY KEY)"))
    await scratch.execute(text(f"GRANT SELECT ON lookup TO {APP_ROLE}"))

    assert (
        await _audit(scratch, global_tables=GLOBAL_REFERENCE_TABLES | {"lookup"}) == []
    )


async def test_a_table_owned_by_the_app_role_is_flagged(scratch):
    await scratch.execute(text("CREATE TABLE usurped (id int PRIMARY KEY)"))
    await scratch.execute(text(f"ALTER TABLE usurped OWNER TO {APP_ROLE}"))

    violations = await _audit(scratch, global_tables={"usurped"})

    assert any("usurped" in v and "owned" in v for v in violations)
