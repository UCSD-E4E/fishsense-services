"""0082, the checked borrow (2026-10-29): the table's borrow columns and
checks, the measurement calibrations serving a borrow (options kept:
security_invoker), and the downgrade puts 0081 back -- but not over a
borrowed row. Its own database: the session's is at head and shared."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api import migrations

pytestmark = pytest.mark.integration

APP_ROLE = "fishsense_app"
VIEWS = ("automatic_measurement_calibrations",)


async def _state(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            one = lambda sql: conn.execute(text(sql))  # noqa: E731
            state = {
                "checks": sorted((await one("""
                    SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conrelid = 'automatic_laser_calibrations'::regclass
                      AND contype = 'c'
                    """)).scalars()),
                "columns": list((await one("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'automatic_laser_calibrations'
                    ORDER BY ordinal_position
                    """)).scalars()),
                "functions": sorted((await one("""
                    SELECT proname FROM pg_proc WHERE proname = 'beam_aim'
                    """)).scalars()),
            }  # fmt: skip
            for view in VIEWS:
                state[view] = (await one(f"""
                    SELECT pg_get_viewdef('{view}'::regclass, true), reloptions,
                           has_table_privilege('{APP_ROLE}', '{view}', 'SELECT')
                    FROM pg_class WHERE oid = '{view}'::regclass
                    """)).one()._tuple()  # fmt: skip
            return state
    finally:
        await engine.dispose()


async def test_0082_and_its_downgrade():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0081")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0082")
        upgraded = await _state(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0081"
        )
        restored = await _state(url)

    assert upgraded["columns"] == [
        *before["columns"], "borrowed_from", "borrow_line_px", "borrow_depth_ratio",
        "borrow_holds",
    ]  # fmt: skip
    assert any("'borrowed'" in c for c in upgraded["checks"])
    definition, options, granted = upgraded["automatic_measurement_calibrations"]
    assert "borrowed_from" in definition
    assert (options, granted) == before["automatic_measurement_calibrations"][1:]
    assert restored == before


async def test_the_downgrade_keeps_a_borrowed_row():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0082")
        async with engine.begin() as conn:
            await conn.execute(text("""
                WITH t AS (INSERT INTO tenants (slug, name) VALUES ('t', 't')
                           RETURNING id),
                     m AS (INSERT INTO missions (tenant_id, source_path, name,
                                                 started_at)
                           SELECT id, '/m', 'm', now() FROM t
                           RETURNING tenant_id, id)
                INSERT INTO automatic_laser_calibrations (
                    tenant_id, mission_id, outcome, algorithm_version,
                    refusal_reason, aim, method)
                SELECT tenant_id, id, 'refused', 'borrow-1', 'no_matching_beam', 1,
                       'borrowed' FROM m
                """))
        await engine.dispose()

        with pytest.raises(RuntimeError, match="borrowed"):
            await asyncio.to_thread(
                command.downgrade, migrations._config(url, APP_ROLE), "0081"
            )
