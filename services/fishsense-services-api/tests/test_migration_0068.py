"""0068 recreates `dive_pipeline_status_rows()` with the laser stage's v3, and
its downgrade puts 0063's body back.

A SQL function's body is frozen text (0029), so the laser stale term's
`IS DISTINCT FROM 2` stays in every existing database until a migration
rewrites it. The parity tests (test_mission_pipeline_status_view.py) run at
head on a fresh database; here, the function as Postgres holds it, before,
after and after the downgrade, and the grants the view's readers (the app,
Superset's analytics role) depend on. Its own database: the session's is at
head and shared, and must not be downgraded under the other tests.
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
ANALYTICS_ROLE = "fishsense_analytics"
LASER_TERM = "ORDER BY p.seq DESC LIMIT 1\n) IS DISTINCT FROM {}"
HEADTAIL_TERM = "p.predictor_version IS DISTINCT FROM 2"


async def _state(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                    SELECT pg_get_functiondef(p.oid) AS body, p.proconfig,
                           p.prosecdef, p.proacl::text AS acl
                    FROM pg_proc p WHERE p.proname = 'dive_pipeline_status_rows'
                    """))).one()
            grants = {}
            for role in (APP_ROLE, ANALYTICS_ROLE):
                grants[role] = (
                    await conn.execute(
                        text(
                            "SELECT has_table_privilege(:r, "
                            "'dive_pipeline_status', 'SELECT')"
                        ),
                        {"r": role},
                    )
                ).scalar_one()
            # The view still reads through the recreated function.
            await conn.execute(text("SELECT * FROM dive_pipeline_status"))
            return {
                "body": row.body,
                "config": row.proconfig,
                "secdef": row.prosecdef,
                "acl": row.acl,
                "grants": grants,
            }
    finally:
        await engine.dispose()


async def test_0068_moves_the_laser_term_to_v3_and_the_downgrade_restores_it():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0067")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0068")
        upgraded = await _state(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0067"
        )
        restored = await _state(url)

    assert before["body"].count(LASER_TERM.format(2)) == 1
    assert LASER_TERM.format(3) not in before["body"]

    assert upgraded["body"].count(LASER_TERM.format(3)) == 1
    assert LASER_TERM.format(2) not in upgraded["body"]
    # Only the laser term moved: head/tail keeps its own version.
    assert HEADTAIL_TERM in upgraded["body"]
    assert upgraded["body"].replace(LASER_TERM.format(3), LASER_TERM.format(2)) == (
        before["body"]
    )
    # CREATE OR REPLACE keeps the function's settings and grants, and the
    # view's readers keep reading it.
    for key in ("config", "secdef", "acl", "grants"):
        assert upgraded[key] == before[key], key
    assert before["grants"] == {APP_ROLE: True, ANALYTICS_ROLE: True}

    assert restored == before
