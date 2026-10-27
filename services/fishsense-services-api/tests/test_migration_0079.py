"""0079, a calibration's error bar (the owner, 2026-10-26): an automatic
calibration may carry its lengths' scale SD, positive and only when
accepted; the views are untouched; the downgrade puts 0078 back. Its own
database: the session's is at head and shared."""

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
VIEWS = ("current_automatic_laser_calibrations", "automatic_measurement_calibrations")


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
            }  # fmt: skip
            for view in VIEWS:
                state[view] = (await one(f"""
                    SELECT pg_get_viewdef('{view}'::regclass, true), reloptions
                    FROM pg_class WHERE oid = '{view}'::regclass
                    """)).one()._tuple()  # fmt: skip
            return state
    finally:
        await engine.dispose()


async def test_0079_and_its_downgrade():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0078")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0079")
        upgraded = await _state(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0078"
        )
        restored = await _state(url)

    assert upgraded["columns"] == [*before["columns"], "scale_sd"]
    assert any("scale_sd > " in c and "'accepted'" in c for c in upgraded["checks"])
    for view in VIEWS:
        assert upgraded[view] == before[view]
    assert restored == before
