"""Every capture's render (0051): which captures are owed one, which missions
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
    restate_port,
    tenant,
)
from fishsense_services_api.automatic_results_store import ForeignCapture
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.renders_store import (
    RENDER_MAX_UNAVAILABLE,
    RendersCatalog,
    renders_at,
    RawRow,
    RenderRow,
    current_renders,
    persist_raws,
    persist_renders,
    render_inputs,
    render_missions,
)

V = 1
MD5 = "0123456789abcdef0123456789abcdef"
BUCKET = "lab-bucket"


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _rows(engine, sql, **params):
    async with engine.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql), params)]


def _render_owed(inputs) -> list:
    return [c.capture_id for c in inputs.captures if c.render_owed]


def _raw_owed(inputs) -> list:
    return [c.capture_id for c in inputs.captures if c.raw_owed]


def _copied(capture_id, **extra) -> RawRow:
    fields = dict(capture_id=capture_id, bucket=BUCKET, key=f"raws/{'c' * 32}.ORF",
                  bytes=15_000_000, md5=MD5)  # fmt: skip
    fields.update(extra)
    return RawRow(**fields)


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

    inputs = await _in(
        app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V
    )

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
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [
        _rendered(a, camera),
        RenderRow(capture_id=b, status="decode_failed", render_version=V,
                  camera_calibration_id=camera),
    ])  # fmt: skip

    owed = await _in(
        app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V
    )
    later = await _in(
        app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V + 1
    )

    assert _render_owed(owed) == [c]
    assert _render_owed(later) == [a, b, c]


async def test_a_raw_missing_from_scratch_is_retried_until_its_third_time(
    owner_engine, app_engine
):
    """A raw not in scratch may come back -- a moved file repaired, scratch
    evicted mid-run -- so it stays owed; the third time it settles, and stops
    holding its mission (review of #144)."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    missing = RenderRow(capture_id=a, status="raw_unavailable", render_version=V,
                        camera_calibration_id=camera)  # fmt: skip

    owed = []
    for _ in range(3):
        await _in(app_engine, lab, persist_renders, mission_id, [missing])
        inputs = await _in(
            app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V
        )
        owed.append([x.capture_id for x in inputs.captures])
    at = await _in(app_engine, lab, renders_at, [a], render_version=V,
                   camera_calibration_id=camera)  # fmt: skip

    assert owed == [[a], [a], []]  # still owed after one miss, and two
    assert RENDER_MAX_UNAVAILABLE == 3
    assert (at[a].status, at[a].settled) == ("raw_unavailable", True)


async def test_owed_captures_come_a_page_at_a_time_in_capture_order(
    owner_engine, app_engine
):
    """A large mission's presigned URLs would not fit one Temporal payload
    (review of #144): it is rendered a page at a time."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    caps = [await capture(owner_engine, lab, mission_id) for _ in range(5)]

    first = await _in(app_engine, lab, render_inputs, mission_id,
                      bucket=BUCKET, render_version=V, limit=2)  # fmt: skip
    rest = await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V,
                     after_number=first.captures[-1].number, limit=10)  # fmt: skip

    assert [c.capture_id for c in first.captures] == caps[:2]
    assert [c.capture_id for c in rest.captures] == caps[2:]


async def test_a_render_is_looked_up_at_its_version_and_camera(
    owner_engine, app_engine
):
    """Not the newest render whatever its camera: a reader asks for the one
    its row names, or the mission's current camera (review of #144)."""
    lab = await tenant(owner_engine)
    device_id = await device(owner_engine, lab)
    mission_id = await mission(owner_engine, lab, device_id=device_id)
    a = await capture(owner_engine, lab, mission_id)
    b = await capture(owner_engine, lab, mission_id)
    first = (await _in(app_engine, lab, render_inputs, mission_id,
                       bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [
        _rendered(a, first), RenderRow(capture_id=b, status="decode_failed",
                                       render_version=V, camera_calibration_id=first),
    ])  # fmt: skip
    second = await camera_calibration(owner_engine, lab, device_id,
                                      [[3100.0, 0, 2000], [0, 3100.0, 1500], [0, 0, 1]],
                                      model="pinhole")  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, second)])

    at_first = await _in(app_engine, lab, renders_at, [a, b], render_version=V,
                         camera_calibration_id=first)  # fmt: skip
    at_second = await _in(app_engine, lab, renders_at, [a, b], render_version=V,
                          camera_calibration_id=second)  # fmt: skip

    assert at_first[a].key == f"renders/r{V}_{first.hex}/{'c' * 32}.JPG"
    assert (at_first[b].status, at_first[b].settled, at_first[b].key) == (
        "decode_failed",
        True,
        None,
    )
    assert at_second[a].camera_calibration_id == second and b not in at_second


async def test_a_corrected_camera_owes_every_capture_a_new_render(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    device_id = await device(owner_engine, lab)
    mission_id = await mission(owner_engine, lab, device_id=device_id)
    a = await capture(owner_engine, lab, mission_id)
    old = (await _in(app_engine, lab, render_inputs, mission_id,
                     bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, old)])

    await camera_calibration(owner_engine, lab, device_id,
                             [[3100.0, 0, 2000], [0, 3100.0, 1500], [0, 0, 1]],
                             model="pinhole")  # fmt: skip
    inputs = await _in(
        app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V
    )

    assert inputs.camera_calibration_id != old
    assert _render_owed(inputs) == [a]


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
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, done, [_rendered(d, camera)])
    await _in(app_engine, lab, persist_raws, done, [_copied(d)])

    got = await _in(app_engine, lab, render_missions, bucket=BUCKET, render_version=V)

    assert [m.mission_id for m in got] == [measured, older]
    assert [(m.owed, m.human_measured) for m in got] == [(1, True), (1, False)]
    assert all(m.created_at is not None for m in got)


# -- what was rendered -------------------------------------------------------------------


async def test_a_render_is_recorded_once_and_is_the_captures_current(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip

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
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip

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
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    with pytest.raises(IntegrityError):
        await _in(app_engine, lab, persist_renders, mission_id,
                  [_rendered(a, camera, **extra)])  # fmt: skip


async def test_rows_are_the_tenants_own_and_append_only(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    other = await tenant(owner_engine, "partner")
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
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
    (got,) = await catalog.render_missions(lab, bucket=BUCKET, render_version=V)
    assert got.mission_id == mission_id


@pytest.mark.parametrize("port", ["unknown", "flat", "dome"])
async def test_a_mission_off_the_corrective_optic_owes_no_render(
    owner_engine, app_engine, port
):
    """A render rectifies the raw as a pinhole camera, which holds only behind
    the corrective optic (2b-2, camera_sql.RECTIFIABLE_MISSION): a flat port's
    frame rendered that way would be the wrong image for every later step and
    for /fish. The control first, then only the port changes."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    await capture(owner_engine, lab, mission_id)
    assert [m.mission_id for m in await _in(
        app_engine, lab, render_missions, bucket=BUCKET, render_version=V
    )] == [mission_id]  # fmt: skip

    await restate_port(owner_engine, mission_id, port)

    assert (
        await _in(app_engine, lab, render_missions, bucket=BUCKET, render_version=V)
        == []
    )
    inputs = await _in(
        app_engine, lab, render_inputs, mission_id, bucket=BUCKET, render_version=V
    )
    assert inputs.captures == []


async def test_current_renders_are_the_newest_rendered_in_a_bucket(
    owner_engine, app_engine
):
    """The one rule for "a capture's render", shared with /fish: the newest
    rendered row by write order, in the given bucket when one is named."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id,
                        bucket=BUCKET, render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [
        _rendered(c, camera, render_version=2, key="renders/r2/c.JPG"),
        _rendered(c, camera, render_version=3, bucket="other", key="renders/r3/c.JPG"),
        RenderRow(capture_id=c, status="raw_unavailable", render_version=4,
                  camera_calibration_id=camera),
    ])  # fmt: skip

    anywhere = await _in(app_engine, lab, current_renders, [c])
    here = await _in(app_engine, lab, current_renders, [c], bucket="lab-bucket")

    assert anywhere[c].key == "renders/r3/c.JPG"
    assert here[c].key == "renders/r2/c.JPG"


# -- the raw copied to the tenant's bucket, and a bucket that changed (2026-10-20) --


async def test_a_rendered_capture_still_owes_its_raw_copy_and_not_a_render(
    owner_engine, app_engine
):
    """Rendered before raws were copied: owed the copy alone, never a second
    render (its stored render must keep matching its recorded MD5)."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, camera)])

    owed = await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                     render_version=V)  # fmt: skip
    missions = await _in(app_engine, lab, render_missions, bucket=BUCKET,
                         render_version=V)  # fmt: skip

    assert (_render_owed(owed), _raw_owed(owed)) == ([], [a])
    assert [(m.mission_id, m.owed) for m in missions] == [(mission_id, 1)]


async def test_a_capture_rendered_and_copied_owes_nothing(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [_rendered(a, camera)])
    await _in(app_engine, lab, persist_raws, mission_id, [_copied(a)])

    owed = await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                     render_version=V)  # fmt: skip

    assert owed.captures == []
    assert await _in(app_engine, lab, render_missions, bucket=BUCKET,
                     render_version=V) == []  # fmt: skip


async def test_a_bucket_that_changed_owes_its_render_and_its_raw_again(
    owner_engine, app_engine
):
    """Review of #151: settled per bucket, or a tenant whose bucket moves loses
    its photos for good. An abstention from the raw itself (it would not
    decode) stands in any bucket."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    b = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [
        _rendered(a, camera),
        RenderRow(capture_id=b, status="decode_failed", render_version=V,
                  camera_calibration_id=camera),
    ])  # fmt: skip
    await _in(app_engine, lab, persist_raws, mission_id, [_copied(a), _copied(b)])

    moved = await _in(app_engine, lab, render_inputs, mission_id,
                      bucket="new-bucket", render_version=V)  # fmt: skip

    assert _render_owed(moved) == [a]
    assert _raw_owed(moved) == [a, b]
    # Recording the new bucket's render waits for 0054, which drops 0051's
    # bucket-blind index (kept through this deploy, review of the stack).


async def test_a_raw_missing_three_times_is_not_owed_a_copy_either(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                        render_version=V)).camera_calibration_id  # fmt: skip
    missing = RenderRow(capture_id=a, status="raw_unavailable", render_version=V,
                        camera_calibration_id=camera)  # fmt: skip
    for _ in range(RENDER_MAX_UNAVAILABLE):
        await _in(app_engine, lab, persist_renders, mission_id, [missing])

    owed = await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                     render_version=V)  # fmt: skip

    assert owed.captures == []


async def test_a_raw_copy_is_recorded_once_per_bucket_and_whole(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    other = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    b = await capture(owner_engine, lab, other)

    written = [await _in(app_engine, lab, persist_raws, mission_id, [_copied(a)])
               for _ in range(2)]  # fmt: skip
    with pytest.raises(ForeignCapture):
        await _in(app_engine, lab, persist_raws, mission_id, [_copied(b)])
    with pytest.raises(IntegrityError):
        await _in(app_engine, lab, persist_raws, mission_id, [_copied(a, md5="x",
                  bucket="other")])  # fmt: skip

    assert written == [1, 0]


async def test_the_catalog_passes_the_bucket_to_current_renders(
    owner_engine, app_engine, seed_memberships
):
    """Review of the stack: the catalog dropped `bucket`, so a caller through
    it got renders from old buckets, which presign to nothing."""
    tenants = await seed_memberships(
        {"service:fishsense-orchestrator": {"lab": "member"}}
    )
    lab = tenants["lab"]
    mission_id = await _plain_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                        render_version=V)).camera_calibration_id  # fmt: skip
    await _in(app_engine, lab, persist_renders, mission_id, [
        _rendered(c, camera, key="renders/here.JPG"),
        _rendered(c, camera, render_version=2, bucket="other", key="renders/there.JPG"),
    ])  # fmt: skip
    catalog = RendersCatalog(app_engine, sub="service:fishsense-orchestrator")

    got = await catalog.current_renders(lab, [c], bucket="lab-bucket")

    assert got[c].key == "renders/here.JPG"


async def test_the_orchestrator_from_before_0052_still_records_its_renders(
    owner_engine, app_engine
):
    """The render backlog ran in production across 0052's deploy: until it
    restarts, the old orchestrator names 0051's index columns in ON CONFLICT,
    which needs that index to still be there (review of the stack)."""
    lab = await tenant(owner_engine)
    mission_id = await _plain_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    camera = (await _in(app_engine, lab, render_inputs, mission_id, bucket=BUCKET,
                        render_version=V)).camera_calibration_id  # fmt: skip
    old_insert = text("""
        INSERT INTO capture_renders (tenant_id, capture_id, status, render_version,
            camera_calibration_id, bucket, key, bytes, md5)
        VALUES (:t, :c, 'rendered', :v, :cam, :b, :k, 1, :md5)
        ON CONFLICT (tenant_id, capture_id, render_version, camera_calibration_id)
            WHERE status = 'rendered' DO NOTHING
        """)

    async with tenant_transaction(app_engine, lab) as conn:
        for _ in range(2):
            await conn.execute(old_insert, {"t": lab, "c": a, "v": V, "cam": camera,
                                            "b": BUCKET, "k": "renders/a.JPG",
                                            "md5": MD5})  # fmt: skip

    rows = await _rows(owner_engine, "SELECT count(*) AS n FROM capture_renders")
    assert rows == [{"n": 1}]
