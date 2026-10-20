"""Every capture's render (0050): which captures are owed one, which missions
first, and the append-only record of what was rendered where.

New in v2 (2026-10-19). A render is a capture's raw rectified to a JPEG,
stored once in the tenant's own bucket; every step after the raw reads it.
It is owed for every canonical capture with no render at the current render
version under its device's current camera calibration -- a corrected camera
owes a new one. Missions with a human measurement come first: their fish are
on the portal's /fish and have no photo until then. Tested on real Postgres:
the RLS, grants and checks are the point.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    camera_calibration,
    capture,
    device,
    fish,
    forget_identities,
    measurement,
    mission,
    tenant,
)
from fishsense_services_api.automatic_results_store import ForeignCapture
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.renders_store import (
    RendersCatalog,
    RenderRow,
    current_renders,
    persist_renders,
    render_inputs,
    render_missions,
)

V = 1
MD5 = "0123456789abcdef0123456789abcdef"


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _rows(engine, sql, **params):
    async with engine.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql), params)]


def _rendered(capture_id, camera, **extra) -> RenderRow:
    fields = dict(capture_id=capture_id, status="rendered", render_version=V,
                  camera_calibration_id=camera, bucket="lab-bucket",
                  key=f"renders/r{V}_{camera.hex}/{'c' * 32}.JPG", bytes=4_000_000,
                  md5=MD5, width=4000, height=3000)  # fmt: skip
    fields.update(extra)
    return RenderRow(**fields)


async def _plain_mission(owner_engine, lab):
    return await mission(owner_engine, lab, device_id=await device(owner_engine, lab))


# -- what is owed ----------------------------------------------------------------------


async def test_every_canonical_capture_is_owed_a_render(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    b = await capture(owner_engine, lab, mission_id)
    await capture(owner_engine, lab, mission_id, canonical=False)

    inputs = await _in(app_engine, lab, render_inputs, mission_id, render_version=V)

    assert [c.capture_id for c in inputs.captures] == [a, b]
    assert inputs.camera_matrix[0][0] == 3000.0
    assert len(inputs.distortion_coefficients) == 5
    assert inputs.camera_calibration_id is not None


async def test_a_render_or_an_abstention_settles_a_capture(owner_engine, app_engine):
    """An abstention (the raw would not decode) is recorded, not re-tried each
    run; a new render version or camera owes the capture again."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    b = await capture(owner_engine, lab, mission_id)
    c = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [
        _rendered(a, camera),
        RenderRow(capture_id=b, status="decode_failed", render_version=V,
                  camera_calibration_id=camera),
    ])  # fmt: skip

    owed = await _in(app_engine, lab, render_inputs, mission_id, render_version=V)
    later = await _in(app_engine, lab, render_inputs, mission_id, render_version=V + 1)

    assert [x.capture_id for x in owed.captures] == [c]
    assert [x.capture_id for x in later.captures] == [a, b, c]


async def test_a_corrected_camera_owes_every_capture_a_new_render(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    device_id = await device(owner_engine, lab)
    mission_id = await mission(owner_engine, lab, device_id=device_id)
    a = await capture(owner_engine, lab, mission_id)
    old = (await _in(app_engine, lab, render_inputs, mission_id,
                     render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, old)])

    await camera_calibration(owner_engine, lab, device_id,
                             [[3100.0, 0, 2000], [0, 3100.0, 1500], [0, 0, 1]],
                             model="pinhole")  # fmt: skip
    inputs = await _in(app_engine, lab, render_inputs, mission_id, render_version=V)

    assert inputs.camera_calibration_id != old
    assert [x.capture_id for x in inputs.captures] == [a]


async def test_missions_with_a_human_measurement_come_first(owner_engine, app_engine):
    """Their fish are on /fish with no photo until rendered."""
    lab = await tenant(owner_engine)
    older = await _plain_mission(owner_engine, lab)
    await capture(owner_engine, lab, older)
    measured, calibration = await calibrated_dive(owner_engine, lab)
    fish_capture = await capture(owner_engine, lab, measured)
    await measurement(owner_engine, lab, fish_capture, await fish(owner_engine, lab),
                      calibration)  # fmt: skip
    done = await _plain_mission(owner_engine, lab)
    d = await capture(owner_engine, lab, done)
    camera = (await _in(app_engine, lab, render_inputs, done,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, done, [_rendered(d, camera)])

    got = await _in(app_engine, lab, render_missions, render_version=V)

    assert [m.mission_id for m in got] == [measured, older]
    assert [(m.owed, m.human_measured) for m in got] == [(1, True), (1, False)]


# -- what was rendered -------------------------------------------------------------------


async def test_a_render_is_recorded_once_and_is_the_captures_current(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        render_version=V)).camera_calibration_id  # fmt: skip

    for _ in range(2):  # a re-run of the same render is not a second row
        await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, camera)])
    got = await _in(app_engine, lab, current_renders, [a])

    rows = await _rows(owner_engine, "SELECT count(*) AS n FROM capture_renders")
    assert rows == [{"n": 1}]
    assert got[a].key == f"renders/r{V}_{camera.hex}/{'c' * 32}.JPG"
    assert (got[a].bucket, got[a].render_version, got[a].camera_calibration_id) == (
        "lab-bucket",
        V,
        camera,
    )


async def test_another_missions_capture_is_refused_and_nothing_is_written(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mine = await _plain_mission(owner_engine, lab)
    other = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mine)
    b = await capture(owner_engine, lab, other)
    camera = (await _in(app_engine, lab, render_inputs, mine,
                        render_version=V)).camera_calibration_id  # fmt: skip

    with pytest.raises(ForeignCapture):
        await _in(app_engine, lab, persist_renders, mine,
                  [_rendered(a, camera), _rendered(b, camera)])  # fmt: skip
    assert await _rows(owner_engine, "SELECT id FROM capture_renders") == []


@pytest.mark.parametrize(
    "extra",
    [dict(md5="xyz"), dict(key=None), dict(bytes=0), dict(render_version=0),
     dict(status="gone"), dict(status="raw_unavailable")],
    ids=["md5", "no-key", "no-bytes", "version-0", "status", "abstention-with-bytes"],
)  # fmt: skip
async def test_a_render_row_is_whole(owner_engine, app_engine, extra):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        render_version=V)).camera_calibration_id  # fmt: skip
    with pytest.raises(IntegrityError):
        await _in(app_engine, lab, persist_renders, mission_id,
                  [_rendered(a, camera, **extra)])  # fmt: skip


async def test_rows_are_the_tenants_own_and_append_only(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    other = await tenant(owner_engine, "partner")
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, camera)])

    assert await _in(app_engine, other, current_renders, [a]) == {}
    for sql in ("UPDATE capture_renders SET bytes = 1", "DELETE FROM capture_renders"):
        with pytest.raises(DBAPIError):
            async with tenant_transaction(app_engine, lab) as conn:
                await conn.execute(text(sql))


# -- the orchestrator's catalog ----------------------------------------------------------

ORCHESTRATOR = "service:fishsense-orchestrator"


async def test_the_catalog_serves_only_member_tenants_and_names_them(
    owner_engine, app_engine, seed_memberships
):
    """The tenant's slug picks its bucket (the tenant stores are by slug)."""
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    lab = tenants["lab"]
    mission_id = await _plain_mission(owner_engine, lab)
    await capture(owner_engine, lab, mission_id)
    catalog = RendersCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert await catalog.tenant_slug(lab) == "lab"
    (got,) = await catalog.render_missions(lab, render_version=V)
    assert got.mission_id == mission_id
