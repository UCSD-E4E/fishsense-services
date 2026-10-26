"""0077: a slate label may say why it was superseded (``superseded_reason``;
mission 436's mirrored labels, 2026-10-26), only when it is superseded; the
downgrade puts 0076's schema back. Its own database: the session's is at head
and shared."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api import migrations

pytestmark = pytest.mark.integration

APP_ROLE = "fishsense_app"
BEFORE = "0076"


async def _columns(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [r[0] for r in await conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'slate_labels' ORDER BY ordinal_position
                """))]  # fmt: skip
    finally:
        await engine.dispose()


async def _reason_without_superseded(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO slate_labels (tenant_id, capture_id, source, completed,
                                          superseded, superseded_reason)
                VALUES (gen_random_uuid(), gen_random_uuid(), 'human', true,
                        false, 'mirrored')
                """))
    finally:
        await engine.dispose()


async def test_0077_adds_the_reason_and_the_downgrade_removes_it():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision=BEFORE)
        before = await _columns(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0077")
        upgraded = await _columns(url)
        # A reason belongs to a superseded label (checked before the foreign
        # keys: a check constraint is evaluated first).
        with pytest.raises(IntegrityError, match="superseded_reason"):
            await _reason_without_superseded(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), BEFORE
        )
        restored = await _columns(url)

    assert upgraded == [*before, "superseded_reason"]
    assert restored == before
