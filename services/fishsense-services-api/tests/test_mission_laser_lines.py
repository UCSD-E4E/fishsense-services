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
    "noise_estimator": "signed_residual_mad",
    "line_confidence": 0.97,
}


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant_and_mission(conn, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )
    mission = await _one(
        conn,
        "INSERT INTO missions (tenant_id, source_path, started_at) "
        "VALUES (:t, '/a', now()) RETURNING id",
        t=tenant,
    )
    return tenant, mission


async def _fit(conn, tenant, mission, **overrides) -> uuid.UUID:
    values = {**FIT, **overrides}
    names = ", ".join(["tenant_id", "mission_id", *values])
    params = ", ".join([":tenant_id", ":mission_id", *(f":{c}" for c in values)])
    return await _one(
        conn,
        f"INSERT INTO mission_laser_lines ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        mission_id=mission,
        **values,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def test_the_app_role_appends_but_never_rewrites_a_fit(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        tenant, mission = await _tenant_and_mission(conn, "lab")

    async with tenant_transaction(app_engine, tenant) as conn:
        await _fit(conn, tenant, mission)

    for statement in (
        "UPDATE mission_laser_lines SET c = 0",
        "DELETE FROM mission_laser_lines",
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
    tenant, mission = await _tenant_and_mission(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _fit(owner, tenant, mission, **overrides)


async def test_current_is_the_latest_fit_per_mission(owner):
    tenant, mission = await _tenant_and_mission(owner, "lab")
    await _fit(owner, tenant, mission, c=-1200.0)
    latest = await _fit(owner, tenant, mission, c=-1180.0)

    current = await _one(
        owner,
        "SELECT id FROM current_mission_laser_lines WHERE mission_id = :d",
        d=mission,
    )

    assert current == latest


async def test_line_confidence_is_an_unbounded_stability_signal(owner):
    """Not a probability: v1's real values run from ~2.6 to ~270 000."""
    tenant, mission = await _tenant_and_mission(owner, "lab")

    await _fit(owner, tenant, mission, line_confidence=270256.98)


@pytest.mark.parametrize(
    "estimator", [None, "median_absolute_deviation"], ids=["missing", "unknown"]
)
async def test_a_fit_says_which_noise_estimator_it_used(owner, estimator):
    """`label_noise_mad`'s scale changed with fishsense-core 4.1.0 (signed
    residuals, about 1.0 sigma, against v1's absolute ones, about 0.59), so a
    fit without its estimator can't be compared with another (migration 0017)."""
    tenant, mission = await _tenant_and_mission(owner, "lab")

    with pytest.raises(IntegrityError):
        async with owner.begin_nested():
            await _fit(owner, tenant, mission, noise_estimator=estimator)
