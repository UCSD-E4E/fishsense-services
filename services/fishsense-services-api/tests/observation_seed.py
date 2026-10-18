"""Seeding helpers for the observation tests: a measured Lite capture and a
committed FishSense Mobile one. Constructors, not fixtures, written the way
the pipeline and the API write them (as the owner for Lite, through
`commit_capture` for Mobile)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import text

from depth_measure_seed import (
    T0,
    calibrate,
    capture,
    depth,
    device,
    dive,
    exec_,
    fish,
    head_tail_label,
    laser_label,
    measurement,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import (
    CaptureFile,
    DeviceMeasurement,
    MobileCapture,
    commit_capture,
)

ALICE = "sub-alice"


async def one(owner_engine, sql: str, **params):
    async with owner_engine.begin() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def lite(
    owner_engine, tenant_id, *, species=None, model=None, at=T0, canonical=True
):
    """A measured Lite capture: device FSL-07, its dive, a laser calibration,
    labels, a depth and a current server measurement. Returns the ids."""
    device_id = await device(owner_engine, tenant_id)
    await exec_(
        owner_engine, "UPDATE devices SET name = 'FSL-07' WHERE id = :d", d=device_id
    )
    dive_id = await dive(owner_engine, tenant_id, device_id=device_id)
    await exec_(
        owner_engine,
        "UPDATE dives SET name = 'Alligator Deep' WHERE id = :d",
        d=dive_id,
    )
    calibration = await calibrate(owner_engine, tenant_id, dive_id)
    capture_id = await capture(owner_engine, tenant_id, dive_id, canonical=canonical)
    await exec_(
        owner_engine,
        "UPDATE captures SET device_id = :dev, captured_at = :at WHERE id = :c",
        dev=device_id,
        at=at,
        c=capture_id,
    )
    laser = await laser_label(owner_engine, tenant_id, capture_id, x=1925.0, y=1310.0)
    head_tail = await head_tail_label(
        owner_engine,
        tenant_id,
        capture_id,
        head=(1600.0, 1125.0),
        tail=(2200.0, 1360.0),
    )
    species_id = None
    if species is not None:
        species_id = await one(
            owner_engine, "SELECT ensure_species(:s, :c)", s=species[0], c=species[1]
        )
    if model is not None:
        await exec_(
            owner_engine,
            "INSERT INTO fish_models (name) VALUES (:n) ON CONFLICT DO NOTHING",
            n=model[0],
        )
        await exec_(
            owner_engine,
            "INSERT INTO fish_model_references (name, known_length_m) VALUES (:n, :len)",
            n=model[0],
            len=model[1],
        )
    fish_id = await fish(
        owner_engine, tenant_id, model=model[0] if model else None, species=species_id
    )
    await depth(owner_engine, tenant_id, capture_id, laser, calibration, depth_m=1.8)
    await measurement(
        owner_engine,
        tenant_id,
        capture_id,
        fish_id,
        calibration,
        length_m=0.305,
        laser_label_id=laser,
        head_tail_label_id=head_tail,
    )
    return capture_id, fish_id


async def mobile(app_engine, owner_engine, tenant_id, *, at=T0, uuid_=None):
    """A FishSense Mobile capture, committed as the API commits one."""
    uploaded_by = await one(
        owner_engine, "SELECT id FROM users WHERE sub = :s", s=ALICE
    )
    client_uuid = uuid_ or uuid.uuid4()
    digest = client_uuid.hex * 2
    files = tuple(
        CaptureFile(
            role=role,
            bucket="fishsense-tenant-lab",
            key=f"mobile/captures/{client_uuid}/{role}",
            sha256=digest,
            size=1000,
            content_type="application/octet-stream",
        )
        for role in ("rgb_jpeg", "rgb_lossless", "depth_f32", "confidence_u8")
    )
    capture_ = MobileCapture(
        client_capture_uuid=client_uuid,
        uploaded_by=uploaded_by,
        captured_at=at,
        device_install_id="install-1",
        device_model="iPhone16,1",
        device_info=None,
        app_version="1.0.0",
        core_version=None,
        model_version=None,
        latitude=32.8665,
        longitude=-117.2546,
        horizontal_accuracy_m=5.0,
        place_name="Scripps Pier",
        orientation="landscape",
        intrinsics=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        camera_pose=None,
        depth_width=256,
        depth_height=192,
        depth_row_bytes=1024,
        confidence_width=256,
        confidence_height=192,
        mask_width=None,
        mask_height=None,
        files=files,
        manifest={},
    )
    measured = DeviceMeasurement(
        client_measurement_uuid=uuid.uuid4(),
        measured_at=at + timedelta(seconds=3),
        length_m=0.42,
        snout=(100.0, 200.0),
        fork=(900.0, 210.0),
        origin="manual",
        algorithm="fishsense-mobile.compute_length",
        algorithm_version="1.0.0",
        core_version=None,
        model_version=None,
    )
    async with tenant_transaction(app_engine, tenant_id) as conn:
        committed = await commit_capture(conn, tenant_id, capture_, [measured])
    return committed.capture_id, client_uuid
