"""0079: a Label Studio project may be retired (``retired_at`` and
``retired_reason``, both or neither; mission 508's pool-test slate project,
2026-10-26); the downgrade puts 0078's schema back, and refuses while a
project is retired rather than forget why. Its own database: the session's is
at head and shared."""

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
BEFORE = "0078"


async def _run(url: str, sql: str):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            return result.all() if result.returns_rows else []
    finally:
        await engine.dispose()


async def _columns(url: str) -> list[str]:
    return [r[0] for r in await _run(url, """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'label_studio_projects' ORDER BY ordinal_position
        """)]  # fmt: skip


def _project(retired_at: str, reason: str) -> str:
    return f"""
        WITH t AS (INSERT INTO tenants (slug, name) VALUES
                   (gen_random_uuid()::text, 'x') RETURNING id)
        INSERT INTO label_studio_projects (tenant_id, kind, ls_project_id, title,
                                           retired_at, retired_reason)
        SELECT id, 'slate', 300745, 't', {retired_at}, {reason} FROM t
        RETURNING ls_project_id
        """


async def test_0079_adds_retirement_and_the_downgrade_removes_it():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        await _run(url, f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'")

        await migrations.upgrade(url, app_role=APP_ROLE, revision=BEFORE)
        before = await _columns(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0079")
        upgraded = await _columns(url)
        # Both or neither: a retirement says why.
        for retired_at, reason in (("now()", "NULL"), ("NULL", "'pool test'")):
            with pytest.raises(IntegrityError, match="retired"):
                await _run(url, _project(retired_at, reason))
        await _run(url, _project("now()", "'pool test'"))
        config = migrations._config(url, APP_ROLE)
        with pytest.raises(RuntimeError, match="1 retired"):
            await asyncio.to_thread(command.downgrade, config, BEFORE)
        still = await _columns(url)
        await _run(url, "DELETE FROM label_studio_projects")
        await asyncio.to_thread(command.downgrade, config, BEFORE)
        restored = await _columns(url)

    assert upgraded == [*before, "retired_at", "retired_reason"]
    assert still == upgraded
    assert restored == before
