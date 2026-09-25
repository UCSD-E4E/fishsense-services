"""Model predictions: laser dot, slate, head/tail -- before any human sees them.

v1 overwrote one prediction per image. Here predictions are append-only with
their provenance (predictor version, checkpoint, core version), and
``current_*`` views give the latest per capture (PLAN.md §4.3). Only migrated
rows may lack a predictor version.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction

MINIMAL = {
    "laser_predictions": {"x": 10.0, "y": 20.0},
    "slate_predictions": {"reference_points": json.dumps([[1.0, 2.0]])},
    "head_tail_predictions": {
        "head_x": 1.0,
        "head_y": 2.0,
        "tail_x": 3.0,
        "tail_y": 4.0,
    },
}
TABLES = list(MINIMAL)


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant_and_capture(conn, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )
    dive = await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "VALUES (:t, :p, now()) RETURNING id",
        t=tenant,
        p=f"/{slug}",
    )
    capture = await _one(
        conn,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, checksum) "
        "VALUES (:t, :d, :p, now(), '0123456789abcdef0123456789abcdef') RETURNING id",
        t=tenant,
        d=dive,
        p=f"/{slug}/1.ORF",
    )
    return tenant, capture


async def _predict(conn, table, tenant, capture, **columns) -> uuid.UUID:
    values = {"predictor_version": 2, **MINIMAL[table], **columns}
    names = ", ".join(["tenant_id", "capture_id", *values])
    params = ", ".join([":tenant_id", ":capture_id", *(f":{c}" for c in values)])
    return await _one(
        conn,
        f"INSERT INTO {table} ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        capture_id=capture,
        **values,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


@pytest.mark.parametrize("table", TABLES)
async def test_the_app_role_appends_but_never_rewrites_a_prediction(
    owner_engine, app_engine, table
):
    async with owner_engine.begin() as conn:
        tenant, capture = await _tenant_and_capture(conn, "lab")

    async with tenant_transaction(app_engine, tenant) as conn:
        await _predict(conn, table, tenant, capture)

    for statement in (f"UPDATE {table} SET checkpoint = 'x'", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, tenant) as conn:
                await conn.execute(text(statement))


@pytest.mark.parametrize("table", TABLES)
async def test_only_a_migrated_prediction_may_lack_a_predictor_version(owner, table):
    tenant, capture = await _tenant_and_capture(owner, "lab")

    await _predict(owner, table, tenant, capture, predictor_version=None, v1_id=5)
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _predict(owner, table, tenant, capture, predictor_version=None)


@pytest.mark.parametrize("table", TABLES)
async def test_current_is_the_latest_prediction_per_capture(owner, table):
    tenant, capture = await _tenant_and_capture(owner, "lab")
    await _predict(owner, table, tenant, capture, predictor_version=1)
    latest = await _predict(owner, table, tenant, capture, predictor_version=2)

    current = await _one(
        owner, f"SELECT id FROM current_{table} WHERE capture_id = :c", c=capture
    )

    assert current == latest


async def test_a_laser_dot_has_both_coordinates_or_neither(owner):
    tenant, capture = await _tenant_and_capture(owner, "lab")

    await _predict(owner, "laser_predictions", tenant, capture, x=None, y=None)
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _predict(owner, "laser_predictions", tenant, capture, y=None)


async def test_a_predicted_head_tail_carries_all_four_points(owner):
    tenant, capture = await _tenant_and_capture(owner, "lab")

    await _predict(
        owner,
        "head_tail_predictions",
        tenant,
        capture,
        status="no_detections",
        head_x=None,
        head_y=None,
        tail_x=None,
        tail_y=None,
    )
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _predict(owner, "head_tail_predictions", tenant, capture, tail_y=None)


async def test_a_new_slate_prediction_has_points_or_a_rejection_not_both(owner):
    tenant, capture = await _tenant_and_capture(owner, "lab")
    table = "slate_predictions"

    await _predict(
        owner, table, tenant, capture, reference_points=None, rejected_reason="no_board"
    )
    for bad in (
        {"reference_points": None},
        {"rejected_reason": "no_board"},
    ):
        with pytest.raises(IntegrityError, match="check"):
            async with owner.begin_nested():
                await _predict(owner, table, tenant, capture, **bad)


async def test_every_gate_verdict_v1_records_is_accepted(owner):
    """Includes ``auto_accepted``, which production holds but the inventory missed."""
    tenant, capture = await _tenant_and_capture(owner, "lab")

    for verdict in (
        "auto_accepted",
        "off_line",
        "along_line_outlier",
        "audit_sample",
        "dive_ineligible",
        "no_prediction",
    ):
        await _predict(
            owner, "laser_predictions", tenant, capture, gate_verdict=verdict
        )
