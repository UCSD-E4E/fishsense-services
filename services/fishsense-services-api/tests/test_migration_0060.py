"""0060 replaces `current_measurements` and its downgrade puts 0059's back.

The downgrade's text is a copy (0060's docstring says why), so a copy that
drifted would install some other view without a word. Here it must restore
0059's definition exactly. Its own database: the session's is at head and
shared, and must not be downgraded under the other tests.
"""

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


async def _definition(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT pg_get_viewdef('current_measurements'::regclass)")
                )
            ).scalar_one()
    finally:
        await engine.dispose()


async def test_the_downgrade_restores_0059s_view():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0059")
        before = await _definition(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0060")
        upgraded = await _definition(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0059"
        )
        restored = await _definition(url)

    assert upgraded != before
    assert restored == before
