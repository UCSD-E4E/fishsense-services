"""0071 widens the three refusal CHECKs, and its downgrade puts 0070's back.

The new reasons are contract v21's (fishsense-core #96): a laser dot that fails
`LaserPoint.is_measurable()`. The widened CHECKs are tested against inserts at
head (test_depth_measure_schema.py, test_laser_depth_store.py,
test_measurement_store.py, test_automatic_results_store.py); here, the
constraint text itself, before, after and after the downgrade, so a downgrade
that drifted from the definitions before it (0025's; 0070's for the
automatic one, which named `zoomed`) would fail. Renumbered 0068 -> 0071
after main's 0069/0070 (review of #361: two heads, and 0070 rewrote the
automatic CHECK too). Its own database: the
session's is at head and shared, and must not be downgraded under the other
tests.
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

CONSTRAINTS = (
    "laser_depth_refusals_reason_check",
    "laser_depth_refusals_depth_m_check",
    "measurement_refusals_reason_check",
    "automatic_measurements_refusal_check",
)


async def _definitions(url: str) -> dict[str, str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = ANY(:names)"
                ),
                {"names": list(CONSTRAINTS)},
            )
            return dict(rows.all())
    finally:
        await engine.dispose()


async def test_0071_widens_the_refusal_checks_and_the_downgrade_restores_them():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'x'"))
        await engine.dispose()

        await migrations.upgrade(url, app_role=APP_ROLE, revision="0070")
        before = await _definitions(url)
        await migrations.upgrade(url, app_role=APP_ROLE, revision="0071")
        upgraded = await _definitions(url)
        await asyncio.to_thread(
            command.downgrade, migrations._config(url, APP_ROLE), "0070"
        )
        restored = await _definitions(url)

    assert set(before) == set(CONSTRAINTS)
    assert "low_parallax" not in "".join(before.values())
    assert "'low_parallax'" in upgraded["laser_depth_refusals_reason_check"]
    assert "'low_parallax'" in upgraded["laser_depth_refusals_depth_m_check"]
    for reason in ("non_finite_depth", "non_positive_depth", "low_parallax"):
        assert f"'{reason}'" in upgraded["measurement_refusals_reason_check"]
    assert "'low_parallax'" in upgraded["automatic_measurements_refusal_check"]
    # The true reason, not remapped to non_positive_depth (review of #361).
    assert "'non_finite_depth'" in upgraded["automatic_measurements_refusal_check"]
    # 0070's zoom gate keeps its reason.
    assert "'zoomed'" in before["automatic_measurements_refusal_check"]
    assert "'zoomed'" in upgraded["automatic_measurements_refusal_check"]
    # The reasons that were there still are.
    for name in CONSTRAINTS:
        for reason in ("non_finite_depth", "non_positive_depth", "zero_length",
                       "non_finite_length", "unparseable_species", "zoomed"):  # fmt: skip
            if f"'{reason}'" in before[name]:
                assert f"'{reason}'" in upgraded[name], (name, reason)
    assert restored == before
