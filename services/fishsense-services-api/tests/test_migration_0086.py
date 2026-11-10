"""0086, a slate pose serves only on its mission's template (2026-11-09):
the measurement calibrations' view keeps its options (security_invoker) and
grant, and the downgrade puts 0085's back. Its own database: the session's
is at head and shared."""

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
VIEW = "automatic_measurement_calibrations"


async def _view(url: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(f"""
                SELECT pg_get_viewdef('{VIEW}'::regclass, true), reloptions,
                       has_table_privilege('{APP_ROLE}', '{VIEW}', 'SELECT')
                FROM pg_class WHERE oid = '{VIEW}'::regclass
                """))).one()._tuple()  # fmt: skip
    finally:
        await engine.dispose()


async def test_0086_and_its_downgrade():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0085")
        before = await _view(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0086")
        upgraded = await _view(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0085"
        )
        restored = await _view(url)

    assert "slate_template_id" not in before[0]
    assert "slate_template_id" in upgraded[0]
    assert upgraded[1:] == before[1:]
    assert restored == before
