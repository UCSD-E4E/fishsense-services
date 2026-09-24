"""Laser depths and measurements: append-only, named inputs (PLAN.md §4.6, §9.13).

v1 overwrote one measurement per (image, fish) and recomputed when its recorded
calibration stopped matching. Here every result is appended with its inputs and
provenance, and **current** is the latest per (capture, fish, source) whose
inputs still hold: its laser calibration is still the dive's effective one, and
its input labels are not superseded. So a recalibration makes old results stale
by itself, history is kept, and a server recompute never displaces a device's
own measurement.
"""

import json
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction

PROVENANCE = {
    "algorithm": "laser_triangulation",
    "algorithm_version": "14.2",
    "core_version": "4.0.0",
}


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


@dataclass
class Scene:
    tenant: uuid.UUID
    dive: uuid.UUID
    capture: uuid.UUID
    fish: uuid.UUID
    laser_label: uuid.UUID
    calibration: uuid.UUID


async def _calibrate(conn, tenant, dive) -> uuid.UUID:
    return await _one(
        conn,
        "INSERT INTO laser_calibrations (tenant_id, dive_id, producer, outcome, "
        "laser_position, laser_axis) VALUES (:t, :d, 'slate', 'accepted', "
        ":p, :a) RETURNING id",
        t=tenant,
        d=dive,
        p=json.dumps([0.1, 0, 0]),
        a=json.dumps([0, 0, 1]),
    )


async def _scene(conn, slug: str = "lab") -> Scene:
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )
    dive = await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "VALUES (:t, '/a', now()) RETURNING id",
        t=tenant,
    )
    capture = await _one(
        conn,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, checksum) "
        "VALUES (:t, :d, '/a/1.ORF', now(), '0123456789abcdef0123456789abcdef') "
        "RETURNING id",
        t=tenant,
        d=dive,
    )
    fish = await _one(
        conn, "INSERT INTO fish (tenant_id) VALUES (:t) RETURNING id", t=tenant
    )
    laser_label = await _one(
        conn,
        "INSERT INTO laser_labels (tenant_id, capture_id, source, ls_project_id, "
        "ls_task_id, completed, x, y) VALUES (:t, :c, 'human', 1, 1, true, 5, 5) "
        "RETURNING id",
        t=tenant,
        c=capture,
    )
    calibration = await _calibrate(conn, tenant, dive)
    return Scene(tenant, dive, capture, fish, laser_label, calibration)


async def _measure(conn, scene: Scene, **columns) -> uuid.UUID:
    values = {
        "source": "server",
        "length_m": 0.42,
        "laser_calibration_id": scene.calibration,
        "laser_label_id": scene.laser_label,
        **PROVENANCE,
        **columns,
    }
    names = ", ".join(["tenant_id", "capture_id", "fish_id", *values])
    params = ", ".join([":tenant_id", ":capture_id", ":fish_id"])
    params += "".join(f", :{c}" for c in values)
    return await _one(
        conn,
        f"INSERT INTO measurements ({names}) VALUES ({params}) RETURNING id",
        tenant_id=scene.tenant,
        capture_id=scene.capture,
        fish_id=scene.fish,
        **values,
    )


async def _current(conn, scene: Scene) -> list[tuple]:
    rows = await conn.execute(
        text(
            "SELECT source, id FROM current_measurements "
            "WHERE capture_id = :c ORDER BY source"
        ),
        {"c": scene.capture},
    )
    return [tuple(r) for r in rows]


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


# --- append-only, with provenance --------------------------------------------------


@pytest.mark.parametrize("table", ["measurements", "laser_depths"])
async def test_results_are_appended_never_rewritten(owner_engine, app_engine, table):
    async with owner_engine.begin() as conn:
        scene = await _scene(conn)

    async with tenant_transaction(app_engine, scene.tenant) as conn:
        if table == "measurements":
            await _measure(conn, scene)
        else:
            await conn.execute(
                text(
                    "INSERT INTO laser_depths (tenant_id, capture_id, laser_label_id, "
                    "laser_calibration_id, depth_m, core_version) "
                    "VALUES (:t, :c, :l, :k, 1.5, '4.0.0')"
                ),
                {
                    "t": scene.tenant,
                    "c": scene.capture,
                    "l": scene.laser_label,
                    "k": scene.calibration,
                },
            )

    for statement in (f"UPDATE {table} SET core_version = 'x'", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, scene.tenant) as conn:
                await conn.execute(text(statement))


@pytest.mark.parametrize(
    "missing",
    ["algorithm", "algorithm_version", "core_version", "laser_calibration_id"],
)
async def test_a_server_measurement_names_what_produced_it(owner, missing):
    scene = await _scene(owner)

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _measure(owner, scene, **{missing: None})


async def test_a_device_measurement_needs_no_server_calibration(owner):
    scene = await _scene(owner)

    await _measure(
        owner, scene, source="device", laser_calibration_id=None, model_version="m-3"
    )


async def test_a_length_is_positive(owner):
    scene = await _scene(owner)

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _measure(owner, scene, length_m=0)


# --- what "current" means (§9.13) --------------------------------------------------


async def test_current_is_the_latest_per_capture_fish_and_source(owner):
    scene = await _scene(owner)
    await _measure(owner, scene, length_m=0.40)
    server = await _measure(owner, scene, length_m=0.41)
    device = await _measure(owner, scene, source="device", laser_calibration_id=None)

    assert await _current(owner, scene) == [("device", device), ("server", server)]


async def test_a_recalibration_makes_old_results_stale_until_remeasured(owner):
    scene = await _scene(owner)
    await _measure(owner, scene)

    recalibrated = await _calibrate(owner, scene.tenant, scene.dive)
    assert await _current(owner, scene) == []

    remeasured = await _measure(owner, scene, laser_calibration_id=recalibrated)
    assert await _current(owner, scene) == [("server", remeasured)]


async def test_a_superseded_input_label_makes_a_result_stale(owner):
    scene = await _scene(owner)
    await _measure(owner, scene)

    await owner.execute(
        text("UPDATE laser_labels SET superseded = true WHERE id = :l"),
        {"l": scene.laser_label},
    )

    assert await _current(owner, scene) == []


async def test_laser_depth_current_is_the_latest_per_capture(owner):
    scene = await _scene(owner)
    insert = (
        "INSERT INTO laser_depths (tenant_id, capture_id, laser_label_id, "
        "laser_calibration_id, depth_m, core_version) "
        "VALUES (:t, :c, :l, :k, :d, '4.0.0') RETURNING id"
    )
    params = {
        "t": scene.tenant,
        "c": scene.capture,
        "l": scene.laser_label,
        "k": scene.calibration,
    }
    await _one(owner, insert, **params, d=1.5)
    latest = await _one(owner, insert, **params, d=1.6)

    assert (
        await _one(
            owner,
            "SELECT id FROM current_laser_depths WHERE capture_id = :c",
            c=scene.capture,
        )
        == latest
    )
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _one(owner, insert, **params, d=-1.0)
