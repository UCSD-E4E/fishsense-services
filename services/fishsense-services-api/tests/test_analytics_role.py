"""The analytics read role: Superset's view of the lab tenant, under RLS.

v1's Superset connected as its own role with SELECT on every table (fishsense-
lite@77e8f8e5 deploy/pg_volumes/scripts/2025-09-02_create_database.sql), and
research as the superuser. Under v2's RLS that role sees nothing (no tenant
set) or, as BYPASSRLS, every tenant. PLAN.md §9.18 and §9.20 are open; this is
their stated lean, pending the owner's decision: a named read role scoped to
the lab tenant through RLS, not BYPASSRLS.

`fishsense_analytics` is a NOLOGIN group. A login (Superset's) is made a
member and bound to the lab by its own setting:

    CREATE ROLE superset LOGIN PASSWORD '...' IN ROLE fishsense_analytics;
    ALTER ROLE superset SET app.tenant_id = '<the lab tenant id>';

(a setting on the group would not reach its members: Postgres applies a role's
settings only at that role's own login). The canonical tenant policy then
shows it the lab's rows, and a restrictive policy on every tenant table it
reads holds it to the lab even if a session sets another tenant -- SQL Lab can
run `SET`. It reads `dive_pipeline_status` and the tables and views under it,
and writes nothing.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from depth_measure_seed import exec_
from test_dive_pipeline_status_view import VIEW, _dive, _tenant

ROLE = "fishsense_analytics"
_logins = itertools.count(1)

Analyst = Callable[[uuid.UUID | None], Awaitable[AsyncEngine]]


@pytest.fixture
async def analyst(postgres, owner_engine) -> AsyncIterator[Analyst]:
    """A login in the analytics group, bound to a tenant (or to none)."""
    made: list[tuple[str, AsyncEngine]] = []

    async def login(tenant_id: uuid.UUID | None) -> AsyncEngine:
        name = f"analyst_{next(_logins)}"
        await exec_(owner_engine, f"CREATE ROLE {name} LOGIN PASSWORD '{name}'")
        await exec_(owner_engine, f"GRANT {ROLE} TO {name}")
        if tenant_id is not None:
            await exec_(
                owner_engine, f"ALTER ROLE {name} SET app.tenant_id = '{tenant_id}'"
            )
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(postgres.port)
        engine = create_async_engine(
            f"postgresql+asyncpg://{name}:{name}@{host}:{port}/{postgres.dbname}"
        )
        made.append((name, engine))
        return engine

    yield login
    for name, engine in made:
        await engine.dispose()
        await exec_(owner_engine, f"DROP ROLE {name}")


async def _dive_ids(engine: AsyncEngine, sql: str = f"SELECT dive_id FROM {VIEW}"):
    async with engine.connect() as conn:
        return set((await conn.execute(text(sql))).scalars())


async def test_the_role_is_a_group_that_cannot_log_in_or_bypass_rls(owner_engine):
    """§9.18's lean: never BYPASSRLS -- that would show every tenant."""
    async with owner_engine.connect() as conn:
        role = (
            await conn.execute(
                text(
                    "SELECT rolcanlogin, rolbypassrls, rolsuper, rolcreaterole, "
                    "rolcreatedb FROM pg_roles WHERE rolname = :r"
                ),
                {"r": ROLE},
            )
        ).one()
    assert tuple(role) == (False, False, False, False, False)


async def test_a_member_sees_the_lab_tenants_dives_and_not_another_tenants(
    owner_engine, analyst
):
    lab = await _tenant(owner_engine)
    partner = await _tenant(owner_engine, "partner")
    mine = {(await _dive(owner_engine, lab)).number for _ in range(2)}
    await _dive(owner_engine, partner)

    engine = await analyst(lab)
    assert await _dive_ids(engine) == mine
    assert await _dive_ids(engine, "SELECT number FROM dives") == mine


async def test_a_member_cannot_widen_its_tenant_by_setting_it(owner_engine, analyst):
    """The binding is RLS, not the setting alone: SQL Lab can `SET` anything,
    and the restrictive policy still holds the member to the lab."""
    lab = await _tenant(owner_engine)
    partner = await _tenant(owner_engine, "partner")
    await _dive(owner_engine, lab)
    await _dive(owner_engine, partner)

    engine = await analyst(lab)
    async with engine.connect() as conn:
        await conn.execute(text(f"SET app.tenant_id = '{partner}'"))
        assert list((await conn.execute(text(f"SELECT dive_id FROM {VIEW}")))) == []
        assert list((await conn.execute(text("SELECT id FROM dives")))) == []


async def test_a_member_bound_to_another_tenant_sees_nothing(owner_engine, analyst):
    """The role is the lab's analytics, not a partner's: bound elsewhere it
    reads no rows (a partner's analytics would be its own role, §9.18)."""
    await _tenant(owner_engine)
    partner = await _tenant(owner_engine, "partner")
    await _dive(owner_engine, partner)

    assert await _dive_ids(await analyst(partner)) == set()


async def test_a_member_with_no_tenant_sees_nothing(owner_engine, analyst):
    lab = await _tenant(owner_engine)
    await _dive(owner_engine, lab)

    assert await _dive_ids(await analyst(None)) == set()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "SELECT id, '/x', now() FROM tenants",
        "UPDATE dives SET name = 'x'",
        "DELETE FROM captures",
        "INSERT INTO species (scientific_name) VALUES ('Sparisoma viride')",
        "TRUNCATE measurements",
    ],
)
async def test_a_member_cannot_write(owner_engine, analyst, statement):
    lab = await _tenant(owner_engine)
    await _dive(owner_engine, lab)
    engine = await analyst(lab)

    with pytest.raises(DBAPIError, match="permission denied"):
        async with engine.begin() as conn:
            await conn.execute(text(statement))


async def test_a_member_cannot_read_who_is_who(owner_engine, analyst):
    """Users and memberships are nobody's analytics."""
    engine = await analyst(await _tenant(owner_engine))
    for table in ("users", "memberships"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with engine.connect() as conn:
                await conn.execute(text(f"SELECT 1 FROM {table}"))


async def test_it_writes_nothing_anywhere(owner_engine):
    async with owner_engine.connect() as conn:
        writable = list(
            (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = "
                        "'public'::regnamespace AND c.relkind IN ('r', 'v', 'p') "
                        "AND has_table_privilege(:r, c.oid, "
                        "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')"
                    ),
                    {"r": ROLE},
                )
            ).scalars()
        )
    assert writable == []


async def test_every_tenant_table_it_reads_holds_it_to_the_lab(owner_engine):
    """A table it may read without the restrictive policy would show it any
    tenant it `SET`: so a grant added later without the policy fails here."""
    async with owner_engine.connect() as conn:
        unbound = list(
            (
                await conn.execute(
                    text("""
                        SELECT c.relname FROM pg_class c
                        WHERE c.relnamespace = 'public'::regnamespace
                          AND c.relkind = 'r'
                          AND has_table_privilege(:r, c.oid, 'SELECT')
                          AND EXISTS (
                              SELECT 1 FROM pg_attribute a
                              WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM pg_policies p
                              WHERE p.schemaname = 'public'
                                AND p.tablename = c.relname
                                AND p.permissive = 'RESTRICTIVE'
                                AND p.cmd IN ('SELECT', 'ALL')
                                AND :r = ANY (p.roles)
                                AND p.qual LIKE '%analytics_tenant_id()%'
                          )
                        ORDER BY 1
                        """),
                    {"r": ROLE},
                )
            ).scalars()
        )
    assert unbound == []


async def test_the_superset_datasets_run_as_a_member(owner_engine, analyst):
    """What Superset runs, as Superset would: the view and everything under it
    are readable, or the dataset errors."""
    from test_superset_datasets import DATASETS

    lab = await _tenant(owner_engine)
    await _dive(owner_engine, lab)
    engine = await analyst(lab)
    for dataset in sorted(DATASETS.glob("*.sql")):
        async with engine.connect() as conn:
            await conn.execute(text(dataset.read_text()))
