"""0083, the unchecked borrow (2026-10-30): an accepted borrow may lack the
slate check (no depth ratio, no holds) but never the line or the lender, and
never one of the check's two columns without the other; the downgrade puts
0082 back -- but not over an unchecked borrow. Its own database: the
session's is at head and shared."""

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

_SEED = """
    WITH t AS (INSERT INTO tenants (slug, name) VALUES ('t', 't') RETURNING id),
         m AS (INSERT INTO missions (tenant_id, source_path, name, started_at)
               SELECT id, '/m', 'm', now() FROM t RETURNING tenant_id, id)
    INSERT INTO automatic_laser_calibrations (
        tenant_id, mission_id, outcome, algorithm_version, refusal_reason, aim,
        method)
    SELECT tenant_id, id, 'refused', 'borrow-1', 'no_matching_beam', 1, 'borrowed'
    FROM m RETURNING id
"""


def _accepted(line_px="1.5", ratio="NULL", holds="NULL", lender="l.id"):
    return f"""
        INSERT INTO automatic_laser_calibrations (
            tenant_id, mission_id, outcome, algorithm_version, aim, method,
            borrowed_from, borrow_line_px, borrow_depth_ratio, borrow_holds)
        SELECT l.tenant_id, l.mission_id, 'accepted', 'borrow-2', 1, 'borrowed',
               {lender}, {line_px}, {ratio}, {holds}
        FROM automatic_laser_calibrations l LIMIT 1
    """


async def _checks(url: str) -> list[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return sorted((await conn.execute(text("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = 'automatic_laser_calibrations'::regclass
                  AND contype = 'c'
                """))).scalars())  # fmt: skip
    finally:
        await engine.dispose()


async def _insert(url: str, sql: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def test_0083_admits_an_unchecked_borrow_and_its_downgrade():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        await _insert(url, f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'")
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0082")
        before = await _checks(url)
        await _insert(url, _SEED)
        with pytest.raises(IntegrityError):  # 0082: the check is required
            await _insert(url, _accepted())

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0083")
        await _insert(url, _accepted())  # line only
        await _insert(url, _accepted(ratio="0.01", holds="2"))  # checked
        for bad in (_accepted(line_px="NULL"), _accepted(lender="NULL"),
                    _accepted(ratio="0.01"), _accepted(holds="2")):  # fmt: skip
            with pytest.raises(IntegrityError):
                await _insert(url, bad)

        with pytest.raises(RuntimeError, match="unchecked"):
            await asyncio.to_thread(
                command.downgrade, migrations._config(url, APP_ROLE), "0082"
            )
        await _insert(url, """DELETE FROM automatic_laser_calibrations
                              WHERE borrow_line_px IS NOT NULL
                                AND borrow_depth_ratio IS NULL""")  # fmt: skip
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0082"
        )
        assert await _checks(url) == before
