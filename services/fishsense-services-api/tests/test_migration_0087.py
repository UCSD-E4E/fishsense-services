"""0087, current lengths a mission at a time (2026-11-09): the index that
finds a mission's current laser calibrations, the calibration pick read
through it, and the current lengths keyed by mission too, so
a mission's read stays inside the mission. The downgrade puts 0086's
definitions back exactly: the lengths view is rebuilt without its column,
and the research export over it with it.
Its own database: the session's is at head and shared."""

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
PICK = "automatic_measurement_calibrations"
LENGTHS = "current_automatic_measurements"
INDEX = "automatic_laser_calibrations_current_idx"


async def _state(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            one = lambda sql: conn.execute(text(sql))  # noqa: E731
            state = {
                "indexes": sorted((await one(f"""
                    SELECT indexname FROM pg_indexes WHERE indexname = '{INDEX}'
                    """)).scalars()),
            }  # fmt: skip
            for view in (PICK, LENGTHS, "automatic_results_export"):
                state[view] = (await one(f"""
                    SELECT pg_get_viewdef('{view}'::regclass, true), reloptions,
                           has_table_privilege('{APP_ROLE}', '{view}', 'SELECT'),
                           ARRAY(SELECT attname::text FROM pg_attribute
                                 WHERE attrelid = '{view}'::regclass AND attnum > 0
                                 ORDER BY attnum)
                    FROM pg_class WHERE oid = '{view}'::regclass
                    """)).one()._tuple()  # fmt: skip
            return state
    finally:
        await engine.dispose()


async def test_0087_and_its_downgrade():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0086")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0087")
        upgraded = await _state(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0086"
        )
        restored = await _state(url)

    assert (before["indexes"], upgraded["indexes"]) == ([], [INDEX])
    pick, options, granted, columns = upgraded[PICK]
    assert (options, granted, columns) == before[PICK][1:]
    assert "current_automatic_laser_calibrations" not in pick
    lengths, options, granted, columns = upgraded[LENGTHS]
    assert columns == [*before[LENGTHS][3], "mission_id"]
    assert (options, granted) == before[LENGTHS][1:3]
    assert "DISTINCT ON (m.tenant_id, c.mission_id, m.capture_id)" in lengths
    assert "current_automatic_laser_calibrations" not in lengths
    # The research export is built on the lengths: untouched.
    assert upgraded["automatic_results_export"] == before["automatic_results_export"]

    # The downgrade puts 0086 back exactly, the export rebuilt with it.
    assert restored == before
