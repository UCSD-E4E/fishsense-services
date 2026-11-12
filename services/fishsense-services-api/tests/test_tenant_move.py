"""Moving missions between tenants (the owner, 2026-11-11: lab's REEF
data dumps belong to the reef tenant).

A move takes the missions asked for and everything that hangs off them --
captures, labels, calibrations, results, Label Studio projects and their sync
cursors -- and the parents only they use (a deployment, a fish). A camera
used by both sides moves, with its calibrations, and the source keeps a copy
of it for the missions that stay (the owner's choice). Numbers are kept. Rows
change tenant and nothing else: a label's coordinates are untouched.

The plan is the whole move run and rolled back, so it names exactly what the
apply writes; its fingerprint covers that, and the apply writes only the plan
reviewed. Pinned here:

* the plan writes nothing and names the missions, cameras and rows moving;
* the apply moves them, keeps numbers and coordinates, and leaves nothing of
  them in the source;
* a shared camera moves and its copy stays, with the staying rows on it;
* a deployment or fish used by both sides, or a mission that would have to
  come along without being asked for, is refused, and nothing is written;
* every stored object of a moving capture must already have its copy in the
  target's bucket;
* a plan that changed since the review writes nothing;
* the keys the move sets aside while it runs are back, exactly as they
  were.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from depth_measure_seed import (
    calibrate,
    capture,
    exec_,
    fish,
    head_tail_label,
    laser_label,
    measurement,
    mission,
)
from fishsense_services_api.mission_backfill import PlanChanged
from fishsense_services_api.tenant_move import (
    MoveRefused,
    apply_move,
    plan_move,
)

LAB_BUCKET = "fishsense-tenant-lab"
REEF_BUCKET = "fishsense-tenant-reef"
K = [[2855.0, 0.0, 2031.0], [0.0, 2881.0, 1447.0], [0.0, 0.0, 1.0]]


async def _one(owner_engine, sql: str, **params):
    async with owner_engine.begin() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant(owner_engine, slug: str) -> uuid.UUID:
    return await _one(owner_engine,
                      "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id",
                      s=slug)  # fmt: skip


async def _device(owner_engine, tenant_id, name: str):
    device_id = await _one(owner_engine,
        "INSERT INTO devices (tenant_id, kind, serial, name) "
        "VALUES (:t, 'lite', :s, :n) RETURNING id",
        t=tenant_id, s=f"SER-{name}", n=name)  # fmt: skip
    camera = await _one(owner_engine,
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
        "camera_matrix, distortion_coefficients) VALUES (:t, :d, 'pinhole', :k, "
        "'[0, 0, 0, 0, 0]') RETURNING id",
        t=tenant_id, d=device_id, k=json.dumps(K))  # fmt: skip
    return device_id, camera


async def _deployment(owner_engine, tenant_id, name: str):
    return await _one(owner_engine,
        "INSERT INTO deployments (tenant_id, name, source_path) VALUES (:t, :n, :n) "
        "RETURNING id", t=tenant_id, n=name)  # fmt: skip


async def _render(owner_engine, tenant_id, capture_id, camera, bucket, key):
    return await _one(owner_engine,
        "INSERT INTO capture_renders (tenant_id, capture_id, status, render_version, "
        "camera_calibration_id, bucket, key, bytes, md5, width, height) "
        "VALUES (:t, :c, 'rendered', 1, :cam, :b, :k, 10, :md5, 4, 3) RETURNING id",
        t=tenant_id, c=capture_id, cam=camera, b=bucket, k=key,
        md5="0" * 32)  # fmt: skip


async def _ls_project(owner_engine, tenant_id, mission_id, ls_project_id):
    await exec_(owner_engine,
        "INSERT INTO label_studio_projects (tenant_id, mission_id, kind, "
        "ls_project_id, title) VALUES (:t, :m, 'laser', :p, 'a dive')",
        t=tenant_id, m=mission_id, p=ls_project_id)  # fmt: skip
    await exec_(owner_engine,
        "INSERT INTO label_studio_sync_cursors (tenant_id, kind, ls_project_id) "
        "VALUES (:t, 'laser', :p)", t=tenant_id, p=ls_project_id)  # fmt: skip


async def _number(owner_engine, table: str, row_id) -> int:
    return await _one(owner_engine, f"SELECT number FROM {table} WHERE id = :i",
                      i=row_id)  # fmt: skip


async def _tenant_of(owner_engine, table: str, row_id) -> uuid.UUID:
    return await _one(owner_engine, f"SELECT tenant_id FROM {table} WHERE id = :i",
                      i=row_id)  # fmt: skip


async def _rows(owner_engine, tenant_id) -> dict[str, int]:
    """How many rows the tenant has in each table the seed writes."""
    tables = ("missions", "captures", "devices", "camera_calibrations",
              "deployments", "laser_labels", "head_tail_labels", "fish",
              "measurements", "laser_calibrations", "capture_renders",
              "label_studio_projects", "label_studio_sync_cursors")  # fmt: skip
    async with owner_engine.connect() as conn:
        return {t: (await conn.execute(
                    text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"),
                    {"t": tenant_id})).scalar_one()
                for t in tables}  # fmt: skip


async def _seeded(owner_engine):
    """Lab with two missions to move (A on the shared camera D and deployment
    P; B on camera E, which only it uses) and one to keep (S, on D). A has a
    labeled, measured frame with renders in both buckets and a Label Studio
    project; S has a render on D's calibration. Reef is empty."""
    lab = await _tenant(owner_engine, "lab")
    reef = await _tenant(owner_engine, "reef")
    d, d_cam = await _device(owner_engine, lab, "FSL-02")
    e, _ = await _device(owner_engine, lab, "FSL-01")
    p = await _deployment(owner_engine, lab, "2023-08 Keys")
    a = await mission(owner_engine, lab, device_id=d)
    await exec_(owner_engine, "UPDATE missions SET deployment_id = :p WHERE id = :m",
                p=p, m=a)  # fmt: skip
    b = await mission(owner_engine, lab, device_id=e)
    s = await mission(owner_engine, lab, device_id=d)
    a_frame = await capture(owner_engine, lab, a)
    b_frame = await capture(owner_engine, lab, b)
    s_frame = await capture(owner_engine, lab, s)
    dot = await laser_label(owner_engine, lab, a_frame, x=1769.5, y=1375.25)
    points = await head_tail_label(owner_engine, lab, a_frame,
                                   head=(10.5, 20.25), tail=(30.0, 40.75))  # fmt: skip
    calibration = await calibrate(owner_engine, lab, a)
    a_fish = await fish(owner_engine, lab)
    await measurement(owner_engine, lab, a_frame, a_fish, calibration,
                      laser_label_id=dot, head_tail_label_id=points)  # fmt: skip
    key = f"renders/r1/{uuid.uuid4().hex}.JPG"
    await _render(owner_engine, lab, a_frame, d_cam, LAB_BUCKET, key)
    await _render(owner_engine, lab, a_frame, d_cam, REEF_BUCKET, key)
    s_render = await _render(owner_engine, lab, s_frame, d_cam, LAB_BUCKET,
                             f"renders/r1/{uuid.uuid4().hex}.JPG")  # fmt: skip
    await _ls_project(owner_engine, lab, a, 4101)
    return {
        "lab": lab, "reef": reef, "d": d, "d_cam": d_cam, "e": e, "p": p,
        "a": a, "b": b, "s": s, "a_frame": a_frame, "b_frame": b_frame,
        "s_frame": s_frame, "dot": dot, "points": points, "fish": a_fish,
        "s_render": s_render,
        "numbers": [await _number(owner_engine, "missions", m) for m in (a, b)],
    }  # fmt: skip


async def _plan(owner_engine, seed, numbers=None):
    async with owner_engine.begin() as conn:
        return await plan_move(
            conn, seed["lab"], seed["reef"], numbers or seed["numbers"],
            source_bucket=LAB_BUCKET, target_bucket=REEF_BUCKET,
        )  # fmt: skip


async def _apply(owner_engine, seed, expect, numbers=None):
    async with owner_engine.begin() as conn:
        return await apply_move(
            conn, seed["lab"], seed["reef"], numbers or seed["numbers"],
            source_bucket=LAB_BUCKET, target_bucket=REEF_BUCKET, expect=expect,
        )  # fmt: skip


async def test_a_plan_writes_nothing_and_names_what_moves(owner_engine):
    seed = await _seeded(owner_engine)
    before = (await _rows(owner_engine, seed["lab"]),
              await _rows(owner_engine, seed["reef"]))  # fmt: skip

    plan = await _plan(owner_engine, seed)

    assert (await _rows(owner_engine, seed["lab"]),
            await _rows(owner_engine, seed["reef"])) == before  # fmt: skip
    assert plan.missions == tuple(sorted(seed["numbers"]))
    assert plan.devices_moved == ("FSL-01",)
    assert plan.devices_split == ("FSL-02",)
    moving = dict(plan.rows)
    assert moving["missions"] == 2
    assert moving["captures"] == 2
    assert moving["deployments"] == 1
    assert moving["devices"] == 2
    assert moving["camera_calibrations"] == 2
    assert moving["laser_labels"] == 1
    assert moving["head_tail_labels"] == 1
    assert moving["fish"] == 1
    assert moving["measurements"] == 1
    assert moving["laser_calibrations"] == 1
    assert moving["capture_renders"] == 2
    assert moving["label_studio_projects"] == 1
    assert moving["label_studio_sync_cursors"] == 1
    # S's render now names the copy of D's calibration, and S its copy of D.
    assert dict(plan.repointed) == {"capture_renders": 1, "missions": 1}
    assert len(plan.fingerprint) == 16


async def test_the_apply_moves_the_missions_and_what_hangs_off_them(owner_engine):
    seed = await _seeded(owner_engine)
    plan = await _plan(owner_engine, seed)

    applied = await _apply(owner_engine, seed, plan.fingerprint)

    assert applied.fingerprint == plan.fingerprint
    reef, lab = seed["reef"], seed["lab"]
    for table, key in (("missions", "a"), ("missions", "b"), ("captures", "a_frame"),
                       ("captures", "b_frame"), ("laser_labels", "dot"),
                       ("head_tail_labels", "points"), ("fish", "fish"),
                       ("deployments", "p"), ("devices", "d"), ("devices", "e"),
                       ("camera_calibrations", "d_cam")):  # fmt: skip
        assert await _tenant_of(owner_engine, table, seed[key]) == reef, (table, key)
    assert await _tenant_of(owner_engine, "missions", seed["s"]) == lab
    # Numbers are kept.
    assert [await _number(owner_engine, "missions", m)
            for m in (seed["a"], seed["b"])] == seed["numbers"]  # fmt: skip
    # Coordinates are untouched.
    async with owner_engine.connect() as conn:
        dot = (await conn.execute(text("SELECT x, y FROM laser_labels WHERE id = :i"),
                                  {"i": seed["dot"]})).one()  # fmt: skip
        points = (await conn.execute(text(
            "SELECT head_x, head_y, tail_x, tail_y FROM head_tail_labels "
            "WHERE id = :i"), {"i": seed["points"]})).one()  # fmt: skip
        cursor = (await conn.execute(text(
            "SELECT tenant_id FROM label_studio_sync_cursors "
            "WHERE ls_project_id = 4101"))).scalar_one()  # fmt: skip
    assert tuple(dot) == (1769.5, 1375.25)
    assert tuple(points) == (10.5, 20.25, 30.0, 40.75)
    assert cursor == reef
    # Nothing of the moved missions is left in lab.
    left = await _rows(owner_engine, lab)
    assert left["missions"] == 1 and left["captures"] == 1
    assert left["laser_labels"] == left["head_tail_labels"] == 0
    assert left["measurements"] == left["fish"] == left["deployments"] == 0
    assert left["label_studio_projects"] == left["label_studio_sync_cursors"] == 0


async def test_a_shared_camera_moves_and_its_copy_stays(owner_engine):
    seed = await _seeded(owner_engine)
    d_number = await _number(owner_engine, "camera_calibrations", seed["d_cam"])
    plan = await _plan(owner_engine, seed)

    await _apply(owner_engine, seed, plan.fingerprint)

    async with owner_engine.connect() as conn:
        copy = (await conn.execute(text(
            "SELECT d.id, d.name, d.serial, d.kind FROM devices d "
            "WHERE d.tenant_id = :t"), {"t": seed["lab"]})).one()  # fmt: skip
        s_device = (await conn.execute(text(
            "SELECT device_id FROM missions WHERE id = :m"),
            {"m": seed["s"]})).scalar_one()  # fmt: skip
        copy_cam = (await conn.execute(text(
            "SELECT id, camera_matrix FROM camera_calibrations WHERE device_id = :d"),
            {"d": copy.id})).one()  # fmt: skip
        s_render_cam = (await conn.execute(text(
            "SELECT camera_calibration_id FROM capture_renders WHERE id = :r"),
            {"r": seed["s_render"]})).scalar_one()  # fmt: skip
    assert copy.id != seed["d"]
    assert (copy.name, copy.serial, copy.kind) == ("FSL-02", "SER-FSL-02", "lite")
    assert s_device == copy.id
    assert copy_cam.camera_matrix == K
    assert s_render_cam == copy_cam.id
    # The original keeps its number; the copy takes a new one.
    assert await _number(owner_engine, "camera_calibrations", seed["d_cam"]) == d_number


async def test_a_deployment_shared_with_a_staying_mission_is_refused(owner_engine):
    seed = await _seeded(owner_engine)
    await exec_(owner_engine, "UPDATE missions SET deployment_id = :p WHERE id = :s",
                p=seed["p"], s=seed["s"])  # fmt: skip

    with pytest.raises(MoveRefused, match="deployments"):
        await _plan(owner_engine, seed)
    assert (await _rows(owner_engine, seed["reef"]))["missions"] == 0


async def test_a_fish_measured_on_both_sides_is_refused(owner_engine):
    seed = await _seeded(owner_engine)
    calibration = await calibrate(owner_engine, seed["lab"], seed["s"])
    await measurement(owner_engine, seed["lab"], seed["s_frame"], seed["fish"],
                      calibration)  # fmt: skip

    with pytest.raises(MoveRefused, match="fish"):
        await _plan(owner_engine, seed)


async def test_a_mission_that_would_have_to_come_along_is_refused(owner_engine):
    """S takes its calibration from A: moving A alone would strand it."""
    seed = await _seeded(owner_engine)
    await exec_(owner_engine,
                "UPDATE missions SET calibration_source_mission_id = :a WHERE id = :s",
                a=seed["a"], s=seed["s"])  # fmt: skip

    with pytest.raises(MoveRefused, match="missions"):
        await _plan(owner_engine, seed)


async def test_every_object_must_have_its_copy_in_the_targets_bucket(owner_engine):
    seed = await _seeded(owner_engine)
    await exec_(owner_engine,
                "DELETE FROM capture_renders WHERE capture_id = :c AND bucket = :b",
                c=seed["a_frame"], b=REEF_BUCKET)  # fmt: skip

    with pytest.raises(MoveRefused, match="capture_renders"):
        await _plan(owner_engine, seed)


async def test_a_changed_plan_writes_nothing(owner_engine):
    seed = await _seeded(owner_engine)
    plan = await _plan(owner_engine, seed)
    await capture(owner_engine, seed["lab"], seed["a"])

    with pytest.raises(PlanChanged):
        await _apply(owner_engine, seed, plan.fingerprint)
    assert (await _rows(owner_engine, seed["reef"]))["missions"] == 0


async def test_a_mission_not_in_the_source_is_refused(owner_engine):
    seed = await _seeded(owner_engine)

    with pytest.raises(MoveRefused, match="999999"):
        await _plan(owner_engine, seed, numbers=[*seed["numbers"], 999999])


async def _key_definitions(owner_engine) -> set[tuple[str, str, str]]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT k.conrelid::regclass::text, k.conname, pg_get_constraintdef(k.oid)
            FROM pg_constraint k
            WHERE k.connamespace = 'public'::regnamespace
            """))  # fmt: skip
        return {tuple(r) for r in rows}


async def test_the_keys_are_as_they_were_after_a_move(owner_engine):
    before = await _key_definitions(owner_engine)
    seed = await _seeded(owner_engine)
    plan = await _plan(owner_engine, seed)
    assert await _key_definitions(owner_engine) == before

    await _apply(owner_engine, seed, plan.fingerprint)

    assert await _key_definitions(owner_engine) == before
