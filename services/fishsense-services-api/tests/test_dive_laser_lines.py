"""Dive laser lines: the within-dive fit of the laser dots, append-only.

v1 kept one overwritten row per dive (``divelaserline``) and uses it to gate
laser auto-accept. It is a within-dive fit only -- never a prior for another
dive (v1's own docstring). Here each fit is appended; ``current`` is the latest
per dive.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction

FIT = {
    "a": 0.6,
    "b": 0.8,
    "c": -1200.0,
    "n_points": 40,
    "inlier_count": 37,
    "inlier_fraction": 0.925,
    "residual_std": 1.4,
    "label_noise_mad": 0.9,
    "line_confidence": 0.97,
}


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant_and_dive(conn, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )
    dive = await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "VALUES (:t, '/a', now()) RETURNING id",
        t=tenant,
    )
    return tenant, dive


async def _fit(conn, tenant, dive, **overrides) -> uuid.UUID:
    values = {**FIT, **overrides}
    names = ", ".join(["tenant_id", "dive_id", *values])
    params = ", ".join([":tenant_id", ":dive_id", *(f":{c}" for c in values)])
    return await _one(
        conn,
        f"INSERT INTO dive_laser_lines ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        dive_id=dive,
        **values,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def test_the_app_role_appends_but_never_rewrites_a_fit(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        tenant, dive = await _tenant_and_dive(conn, "lab")

    async with tenant_transaction(app_engine, tenant) as conn:
        await _fit(conn, tenant, dive)

    for statement in (
        "UPDATE dive_laser_lines SET c = 0",
        "DELETE FROM dive_laser_lines",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, tenant) as conn:
                await conn.execute(text(statement))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"inlier_count": 41}, id="more-inliers-than-points"),
        pytest.param({"inlier_fraction": 1.2}, id="fraction-above-one"),
        pytest.param({"line_confidence": -0.1}, id="negative-confidence"),
        pytest.param({"residual_std": -1.0}, id="negative-residual"),
    ],
)
async def test_fit_statistics_stay_in_range(owner, overrides):
    tenant, dive = await _tenant_and_dive(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _fit(owner, tenant, dive, **overrides)


async def test_current_is_the_latest_fit_per_dive(owner):
    tenant, dive = await _tenant_and_dive(owner, "lab")
    await _fit(owner, tenant, dive, c=-1200.0)
    latest = await _fit(owner, tenant, dive, c=-1180.0)

    current = await _one(
        owner, "SELECT id FROM current_dive_laser_lines WHERE dive_id = :d", d=dive
    )

    assert current == latest


async def test_line_confidence_is_an_unbounded_stability_signal(owner):
    """Not a probability: v1's real values run from ~2.6 to ~270 000."""
    tenant, dive = await _tenant_and_dive(owner, "lab")

    await _fit(owner, tenant, dive, line_confidence=270256.98)
