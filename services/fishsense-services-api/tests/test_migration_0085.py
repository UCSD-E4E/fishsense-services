"""0085, regional species (2026-10-30): the fish OBIS records most where we
dive, 45 per region, readable by the app, and the list a species row chose
from; the downgrade drops both. Its own database: the session's is at head
and shared."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api import migrations
from fishsense_services_api.regions import REGIONS

pytestmark = pytest.mark.integration

APP_ROLE = "fishsense_app"


async def _all(url, sql):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).all()
    finally:
        await engine.dispose()


async def test_0085_seeds_every_regions_list_and_its_downgrade_drops_it():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0085")

        counts = dict(await _all(url, """
            SELECT region, count(*) FROM regional_species GROUP BY region"""))  # fmt: skip
        assert counts == {region: 45 for region in REGIONS}
        sharks = await _all(url, """
            SELECT region FROM regional_species
            WHERE scientific_name = 'Ginglymostoma cirratum' ORDER BY region""")  # fmt: skip
        assert [r for (r,) in sharks] == ["Caribbean", "Florida Keys, USA"]
        ((granted,),) = await _all(url, f"""
            SELECT has_table_privilege('{APP_ROLE}', 'regional_species', 'SELECT')""")  # fmt: skip
        assert granted
        columns = await _all(url, """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'automatic_species_predictions'
              AND column_name LIKE 'species_%' ORDER BY 1""")  # fmt: skip
        assert [c for (c,) in columns] == ["species_list_version", "species_region"]

        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0084"
        )
        ((exists,),) = await _all(
            url, "SELECT to_regclass('regional_species') IS NOT NULL"
        )
        assert not exists
        assert not await _all(url, """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'automatic_species_predictions'
              AND column_name LIKE 'species_%'""")  # fmt: skip
