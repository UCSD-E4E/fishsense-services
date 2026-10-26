"""0074, head/tail v5's keypoints: the table and its current view, the
keypoints column on automatic lengths (and on `current_automatic_measurements`,
which lists its columns), the two review refusals -- and the downgrade puts
0073's schema back. Its own database: the session's is at head and shared.
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


async def _state(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            one = lambda sql: conn.execute(text(sql))  # noqa: E731
            return {
                "view_columns": [r[0] for r in await one("""
                        SELECT attname FROM pg_attribute
                        WHERE attrelid = 'current_automatic_measurements'::regclass
                          AND attnum > 0 AND NOT attisdropped ORDER BY attnum
                        """)],
                "view_options": (await one("""
                        SELECT reloptions FROM pg_class
                        WHERE oid = 'current_automatic_measurements'::regclass
                        """)).scalar_one(),
                "app_reads_view": (await one(f"""
                        SELECT has_table_privilege('{APP_ROLE}',
                            'current_automatic_measurements', 'SELECT')
                        """)).scalar_one(),
                "refusals": (await one("""
                        SELECT pg_get_constraintdef(oid) FROM pg_constraint
                        WHERE conname = 'automatic_measurements_refusal_check'
                        """)).scalar_one(),
                "table": (
                    await one(
                        "SELECT to_regclass('automatic_keypoint_predictions') IS NOT NULL"
                    )
                ).scalar_one(),
                "app_writes_table": (await one(f"""
                        SELECT to_regclass('automatic_keypoint_predictions') IS NOT NULL
                           AND has_table_privilege('{APP_ROLE}',
                               'automatic_keypoint_predictions', 'INSERT')
                        """)).scalar_one(),
            }
    finally:
        await engine.dispose()


async def test_0074_adds_the_keypoints_and_the_downgrade_removes_them():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0073")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0074")
        upgraded = await _state(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0073"
        )
        restored = await _state(url)

    assert upgraded["table"] and upgraded["app_writes_table"]
    assert upgraded["view_columns"] == [
        *before["view_columns"],
        "automatic_keypoint_prediction_id",
    ]
    # RLS still applies to the caller, and the app still reads it.
    assert upgraded["view_options"] == before["view_options"]
    assert "security_invoker=true" in before["view_options"]
    assert upgraded["app_reads_view"] is True
    for reason in ("keypoints_disagree", "keypoints_refused"):
        assert f"'{reason}'" in upgraded["refusals"]
        assert f"'{reason}'" not in before["refusals"]
    assert "'beam_moved'" in upgraded["refusals"]  # 0073's kept

    assert restored == before
