"""Migration 0024: what a calibration was fitted against, and
an operator's clear of a refusal -- both append-only.

v1 recorded neither. Its extrinsics row named no target, so a calibration's
geometry could not be traced to the slate template or the board (and board
version) that produced it; PLAN.md §4.3 asks for "the target plus its geometry
version". And v1 cleared a refusal by nulling three dive columns
(`_clear_refusal`, `DELETE /dives/{id}/calibration-refused/`); v2's refusal is
a row in the append-only `laser_calibrations`, so an operator's clear is a row
of its own, in `laser_calibration_refusal_clears`, rather than an UPDATE the
app role is never granted (port-plan: append-only stays append-only).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _scene(conn):
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES ('lab', 'lab') RETURNING id"
    )
    dive = await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "VALUES (:t, '/d', now()) RETURNING id",
        t=tenant,
    )
    slate = await _one(
        conn,
        "INSERT INTO slate_templates (name, dpi, reference_points) "
        "VALUES (:n, 300, '[]') RETURNING id",
        n=f"H-Slate {uuid.uuid4()}",
    )
    board = await _one(
        conn,
        "INSERT INTO calibration_targets (name, interior_rows, interior_cols, "
        "pitch_x_m, pitch_y_m) VALUES (:n, 10, 14, 0.042, 0.042) "
        "RETURNING id",
        n=f"E4E Checkerboard {uuid.uuid4()}",
    )
    return tenant, dive, slate, board


async def _refused(conn, tenant, dive, **columns):
    names = ", ".join(["tenant_id", "dive_id", "producer", "outcome",
                       "refusal_reason", *columns])  # fmt: skip
    params = ", ".join([":tenant_id", ":dive_id", "'slate'", "'refused'",
                        "'insufficient laser points (1 < 2)'",
                        *(f":{c}" for c in columns)])  # fmt: skip
    return await _one(
        conn,
        f"INSERT INTO laser_calibrations ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        dive_id=dive,
        **columns,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def test_a_calibration_names_the_slate_or_the_board_it_was_fitted_against(
    owner,
):
    tenant, dive, slate, board = await _scene(owner)

    by_slate = await _refused(owner, tenant, dive, slate_template_id=slate)
    by_board = await _refused(owner, tenant, dive, calibration_target_id=board)

    rows = (
        await owner.execute(
            text(
                "SELECT id, slate_template_id, calibration_target_id "
                "FROM laser_calibrations ORDER BY seq"
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        (by_slate, slate, None),
        (by_board, None, board),
    ]


async def test_a_calibration_cannot_name_two_targets(owner):
    """One fit, one plane: a row naming both could not say which it used."""
    tenant, dive, slate, board = await _scene(owner)

    with pytest.raises(IntegrityError, match="laser_calibrations_one_target_check"):
        await _refused(
            owner, tenant, dive, slate_template_id=slate, calibration_target_id=board
        )


async def test_a_clear_is_appended_and_never_rewritten(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        tenant, dive, slate, _ = await _scene(conn)
        refusal = await _refused(conn, tenant, dive, slate_template_id=slate)

    async with tenant_transaction(app_engine, tenant) as conn:
        await conn.execute(
            text(
                "INSERT INTO laser_calibration_refusal_clears "
                "(tenant_id, laser_calibration_id, reason) VALUES (:t, :r, 'retry')"
            ),
            {"t": tenant, "r": refusal},
        )

    for statement in (
        "UPDATE laser_calibration_refusal_clears SET reason = 'x'",
        "DELETE FROM laser_calibration_refusal_clears",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, tenant) as conn:
                await conn.execute(text(statement))


async def test_a_refusal_is_cleared_at_most_once(owner):
    tenant, dive, slate, _ = await _scene(owner)
    refusal = await _refused(owner, tenant, dive, slate_template_id=slate)
    insert = (
        "INSERT INTO laser_calibration_refusal_clears "
        "(tenant_id, laser_calibration_id) VALUES (:t, :r)"
    )
    await owner.execute(text(insert), {"t": tenant, "r": refusal})

    with pytest.raises(IntegrityError, match="duplicate key"):
        await owner.execute(text(insert), {"t": tenant, "r": refusal})


async def test_a_clear_cannot_point_into_another_tenant(owner):
    tenant, dive, slate, _ = await _scene(owner)
    refusal = await _refused(owner, tenant, dive, slate_template_id=slate)
    other = await _one(
        owner, "INSERT INTO tenants (slug, name) VALUES ('reef', 'reef') RETURNING id"
    )

    with pytest.raises(IntegrityError, match="foreign key"):
        await owner.execute(
            text(
                "INSERT INTO laser_calibration_refusal_clears "
                "(tenant_id, laser_calibration_id) VALUES (:t, :r)"
            ),
            {"t": other, "r": refusal},
        )


async def test_another_tenants_clears_are_invisible(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        tenant, dive, slate, _ = await _scene(conn)
        refusal = await _refused(conn, tenant, dive, slate_template_id=slate)
        await conn.execute(
            text(
                "INSERT INTO laser_calibration_refusal_clears "
                "(tenant_id, laser_calibration_id) VALUES (:t, :r)"
            ),
            {"t": tenant, "r": refusal},
        )
        other = await _one(
            conn,
            "INSERT INTO tenants (slug, name) VALUES ('reef', 'reef') RETURNING id",
        )

    async with tenant_transaction(app_engine, other) as conn:
        seen = await _one(conn, "SELECT count(*) FROM laser_calibration_refusal_clears")
    assert seen == 0
