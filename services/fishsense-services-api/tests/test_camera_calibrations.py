"""Camera calibrations: intrinsics per device, append-only (PLAN.md §4.3).

Two halves of a rig are calibrated separately (§2.7): the camera here, the laser
per dive. A camera calibration names its camera model, because a flat-port
camera is axial, not a pinhole (§8), and the medium and coordinate frame it was
taken in. Unknowns stay NULL -- migrated v1 rows never recorded them -- rather
than being guessed.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction

MATRIX = [[2800.0, 0.0, 2000.0], [0.0, 2800.0, 1500.0], [0.0, 0.0, 1.0]]
DISTORTION = [0.1, -0.2, 0.0, 0.0, 0.05]


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant_with_device(conn, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )
    device = await _one(
        conn,
        "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
        "RETURNING id",
        t=tenant,
        s=f"{slug}-TG6",
    )
    return tenant, device


async def _calibration(conn, tenant, device, **columns) -> uuid.UUID:
    values = {
        "camera_matrix": json.dumps(MATRIX),
        "distortion_coefficients": json.dumps(DISTORTION),
        **columns,
    }
    names = ", ".join(["tenant_id", "device_id", *values])
    params = ", ".join([":tenant_id", ":device_id", *(f":{c}" for c in values)])
    return await _one(
        conn,
        f"INSERT INTO camera_calibrations ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        device_id=device,
        **values,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def test_the_app_role_appends_but_never_rewrites_a_calibration(
    owner_engine, app_engine
):
    async with owner_engine.begin() as conn:
        tenant, device = await _tenant_with_device(conn, "lab")

    async with tenant_transaction(app_engine, tenant) as conn:
        await _calibration(conn, tenant, device, medium="water")

    for statement in (
        "UPDATE camera_calibrations SET medium = 'air'",
        "DELETE FROM camera_calibrations",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, tenant) as conn:
                await conn.execute(text(statement))


async def test_the_camera_matrix_must_be_three_by_three(owner):
    tenant, device = await _tenant_with_device(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _calibration(
                owner, tenant, device, camera_matrix=json.dumps(MATRIX[:2])
            )


async def test_an_axial_refractive_camera_must_name_its_port_model(owner):
    tenant, device = await _tenant_with_device(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _calibration(owner, tenant, device, camera_model="axial_refractive")
    await _calibration(
        owner,
        tenant,
        device,
        camera_model="axial_refractive",
        port_model="flat_port_pinax",
        port_model_version="1",
        medium="air",
        coordinate_frame="raw_sensor",
    )


async def test_an_unknown_medium_is_allowed_but_a_wrong_one_is_not(owner):
    tenant, device = await _tenant_with_device(owner, "lab")

    await _calibration(owner, tenant, device)  # medium unknown: a migrated v1 row
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _calibration(owner, tenant, device, medium="vacuum")


async def test_current_gives_the_latest_per_device_within_the_tenant_only(
    owner_engine, app_engine
):
    async with owner_engine.begin() as conn:
        lab, lab_device = await _tenant_with_device(conn, "lab")
        partner, partner_device = await _tenant_with_device(conn, "partner")
        await _calibration(conn, lab, lab_device, medium="water", rms_px=0.9)
        await _calibration(conn, lab, lab_device, medium="air", rms_px=0.4)
        await _calibration(conn, partner, partner_device, medium="water")

    async with tenant_transaction(app_engine, lab) as conn:
        rows = (
            await conn.execute(
                text("SELECT device_id, medium FROM current_camera_calibrations")
            )
        ).all()

    assert rows == [(lab_device, "air")]
