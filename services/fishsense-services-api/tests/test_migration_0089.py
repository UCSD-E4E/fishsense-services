"""0089, the ``fish_model`` refusal (2026-11-10): an automatic length may be
withdrawn as a pool trial's fish model. The downgrade puts 0088's refusals
back exactly. Its own database: the session's is at head and shared."""

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


async def _check(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("""
                    SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conname = 'automatic_measurements_refusal_check'
                    """))).scalar_one()
    finally:
        await engine.dispose()


async def test_0089_and_its_downgrade():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0088")
        before = await _check(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0089")
        upgraded = await _check(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0088"
        )
        restored = await _check(url)

    assert "'fish_model'" not in before
    assert "'fish_model'" in upgraded
    assert upgraded.replace(", 'fish_model'::text", "") == before
    assert restored == before
