"""0073, the partial run: `mission_beam_moves`, `before_beam_move()`, the two
human work views gated by it, and a `beam_moved` refusal for automatic
lengths -- and the downgrade puts every one back.

The work views were last rebuilt by renaming columns in place (0046), so
their text lives only in the database: the migration edits the definition
Postgres holds rather than retyping it. Here, the views as Postgres holds
them before, after and after the downgrade, and the grants their readers
depend on. Its own database: the session's is at head and shared.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api import migrations

pytestmark = pytest.mark.integration

APP_ROLE = "fishsense_app"
VIEWS = ("measurement_work", "laser_depth_work")
GATE = "before_beam_move(c.tenant_id, c.mission_id, c.captured_at)"


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
                    {"f": "before_beam_move"},
                )
            ).scalar_one()
            return state
    finally:
        await engine.dispose()


async def _gated(url: str) -> list[bool]:
    """A mission with a move after 10:00: frames at 09:00, 10:00 (the last on
    the first aim), 10:00:30 (in the window) and 11:00."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            t = (
                await conn.execute(
                    text(
                        "INSERT INTO tenants (slug, name) VALUES ('t', 't') RETURNING id"
                    )
                )
            ).scalar_one()
            m = (
                await conn.execute(
                    text(
                        "INSERT INTO missions (tenant_id, source_path, started_at) "
                        "VALUES (:t, 'p', '2024-12-06T09:00Z') RETURNING id"
                    ),
                    {"t": t},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO mission_beam_moves (tenant_id, mission_id, moved_after, "
                    "moved_before, detector_version) VALUES (:t, :m, '2024-12-06T10:00Z', "
                    "'2024-12-06T10:01Z', '1')"
                ),
                {"t": t, "m": m},
            )
            other = (
                await conn.execute(
                    text(
                        "INSERT INTO missions (tenant_id, source_path, started_at) "
                        "VALUES (:t, 'q', '2024-12-06T09:00Z') RETURNING id"
                    ),
                    {"t": t},
                )
            ).scalar_one()
            out = [
                (
                    await conn.execute(
                        text("SELECT before_beam_move(:t, :m, :at)"),
                        {"t": t, "m": m, "at": at},
                    )
                ).scalar_one()
                for at in (
                    datetime.fromisoformat("2024-12-06T09:00Z"),
                    datetime.fromisoformat("2024-12-06T10:00Z"),
                    datetime.fromisoformat("2024-12-06T10:00:30Z"),
                    datetime.fromisoformat("2024-12-06T11:00Z"),
                )
            ]
            out.append(
                (
                    await conn.execute(
                        text("SELECT before_beam_move(:t, :m, '2024-12-06T11:00Z')"),
                        {"t": t, "m": other},
                    )
                ).scalar_one()
            )
            await conn.execute(text("DELETE FROM mission_beam_moves"))
            await conn.execute(text("DELETE FROM missions"))
            await conn.execute(text("DELETE FROM tenants"))
            return out
    finally:
        await engine.dispose()


async def test_0073_gates_the_work_views_and_the_downgrade_restores_them():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0072")
        before = await _state(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0073")
        upgraded = await _state(url)
        gated = await _gated(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0072"
        )
        restored = await _state(url)

    # Up to the last dot on the first aim: in; the window and after: out; a
    # mission with no move: in.
    assert gated == [True, True, False, False, True]
    for view in VIEWS:
        assert GATE not in before[view]
        assert upgraded[view].count(GATE) == 1, view
        # The gate is the only change.
        assert upgraded[view].replace(f"{GATE} AND ", "") == before[view], view
        assert upgraded[f"{view}_grant"] is True
    # RLS still applies to the caller (CREATE OR REPLACE drops view options).
    assert upgraded["options"] == before["options"]
    assert all("security_invoker=true" in o for o in before["options"])
    assert "'beam_moved'" in upgraded["refusal_check"]
    assert "'beam_moved'" not in before["refusal_check"]
    assert (before["function"], upgraded["function"]) == (0, 1)

    assert restored == before
