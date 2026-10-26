"""0070, the zoom gate: `focal_length_calibrated()`, the two human work views
gated by it, and a `zoomed` refusal for automatic lengths -- and the
downgrade puts every one back.

The work views were last rebuilt by renaming columns in place (0046), so
their text lives only in the database: the migration edits the definition
Postgres holds rather than retyping it. Here, the views as Postgres holds
them before, after and after the downgrade, and the grants their readers
depend on. Its own database: the session's is at head and shared.
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
VIEWS = ("measurement_work", "laser_depth_work")
GATE = "focal_length_calibrated(c.focal_length_mm)"


async def _state(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            state = {}
            for view in VIEWS:
                state[view] = (
                    await conn.execute(
                        text("SELECT pg_get_viewdef(CAST(:v AS regclass), true)"),
                        {"v": view},
                    )
                ).scalar_one()
                state[f"{view}_grant"] = (
                    await conn.execute(
                        text("SELECT has_table_privilege(:r, :v, 'SELECT')"),
                        {"r": APP_ROLE, "v": view},
                    )
                ).scalar_one()
            state["options"] = (await conn.execute(text("""
                        SELECT array_agg(relname || ':' || array_to_string(reloptions, ',')
                                         ORDER BY relname)
                        FROM pg_class WHERE relname IN ('measurement_work',
                                                        'laser_depth_work')
                        """))).scalar_one()
            state["refusal_check"] = (await conn.execute(text("""
                        SELECT pg_get_constraintdef(oid) FROM pg_constraint
                        WHERE conname = 'automatic_measurements_refusal_check'
                        """))).scalar_one()
            state["function"] = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :f"),
                    {"f": "focal_length_calibrated"},
                )
            ).scalar_one()
            return state
    finally:
        await engine.dispose()


async def _calibrated(url: str, values) -> list[bool]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [
                (
                    await conn.execute(
                        text(
                            "SELECT focal_length_calibrated(CAST(:f AS double precision))"
                        ),
                        {"f": v},
                    )
                ).scalar_one()
                for v in values
            ]
    finally:
        await engine.dispose()


async def test_0070_gates_the_work_views_and_the_downgrade_restores_them():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0069")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0070")
        upgraded = await _state(url)
        # Unknown (a phone, or not read) and the TG-6/TG-7 wide end pass;
        # anything zoomed does not.
        calibrated = await _calibrated(url, [None, 4.5, 4.504, 4.51, 5.05, 18.0])
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0069"
        )
        restored = await _state(url)

    assert calibrated == [True, True, True, False, False, False]
    for view in VIEWS:
        assert GATE not in before[view]
        assert upgraded[view].count(GATE) == 1, view
        # The gate is the only change.
        assert upgraded[view].replace(f"{GATE} AND ", "") == before[view], view
        assert upgraded[f"{view}_grant"] is True
    # RLS still applies to the caller (CREATE OR REPLACE drops view options).
    assert upgraded["options"] == before["options"]
    assert all("security_invoker=true" in o for o in before["options"])
    assert "'zoomed'" in upgraded["refusal_check"]
    assert "'zoomed'" not in before["refusal_check"]
    assert (before["function"], upgraded["function"]) == (0, 1)

    assert restored == before
