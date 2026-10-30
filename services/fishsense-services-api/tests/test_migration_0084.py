"""0084, species length references (2026-10-30): the 13 target species'
published maximum and typical range, readable by the app; the downgrade
drops the table. Its own database: the session's is at head and shared."""

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


async def _one(url, sql):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).all()
    finally:
        await engine.dispose()


async def test_0084_seeds_the_references_and_its_downgrade_drops_them():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0084")

        rows = await _one(url, """
            SELECT scientific_name, max_length_m, log_median, log_mad, typical_n
            FROM species_length_references ORDER BY scientific_name""")  # fmt: skip
        assert len(rows) == 13
        hogfish = next(r for r in rows if r[0] == "Lachnolaimus maximus")
        assert hogfish[1:] == (0.91, -1.4823, 0.1473, 2977)
        assert sum(r[4] for r in rows) == 23024
        ((granted,),) = await _one(url, f"""
            SELECT has_table_privilege('{APP_ROLE}', 'species_length_references',
                                       'SELECT')""")  # fmt: skip
        assert granted

        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0083"
        )
        ((exists,),) = await _one(
            url, "SELECT to_regclass('species_length_references') IS NOT NULL"
        )
        assert not exists
