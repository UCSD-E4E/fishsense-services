"""The shadow calibration's tables and inputs (0060; docs/shadow-size-constancy.md).

The shadow stage runs size constancy beside production's laser calibration and
**feeds nothing**: its two tables are read by no view, function or production
query, and writing them leaves every calibration, depth and measurement view
exactly as it was (§9.1). What it reads follows production's rules exactly:
a slate frame is a capture with a live slate label (or one the detector calls
slate); its label dot is the lowest-numbered live laser label, whatever its
source; the reference is the mission's `effective_laser_calibrations` row.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from _slate_calibration_seed import (
    capture,
    device_with_camera,
    laser_calibration,
    laser_label,
    mission,
    run,
    slate_label,
    slate_presence,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_calibration_store import slate_calibration_inputs
from fishsense_services_api.shadow_calibration_store import (
    ShadowCalibrationRow,
    ShadowFrameRow,
    next_mission_for_shadow_calibration,
    persist_shadow_calibrations,
    shadow_calibration_inputs,
)
from fishsense_services_api.slate_presence_store import slate_frames

TABLES = ("shadow_laser_calibrations", "shadow_calibration_frames")
HV = 3


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _rows(engine, sql, **params):
    async with engine.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql), params)]


async def _mission(owner_engine, lab, **kwargs):
    device, _ = await device_with_camera(owner_engine, lab)
    return await mission(owner_engine, lab, device=device, **kwargs)


async def _automatic_dot(owner_engine, lab, capture_id, x, y, confidence, version=HV):
    async with owner_engine.begin() as conn:
        return (await conn.execute(text("""
            INSERT INTO automatic_head_tail_predictions (tenant_id, capture_id, status,
                predictor_version, laser_x, laser_y, laser_confidence, slate_probability)
            VALUES (:t, :c, 'slate_frame', :v, :x, :y, :conf, 0.9) RETURNING id
            """), dict(t=lab, c=capture_id, v=version, x=x, y=y,
                       conf=confidence))).scalar_one()  # fmt: skip


def _row(**over) -> ShadowCalibrationRow:
    fields = dict(dot_source="labels", variant="gated", outcome="refused",
                  refusal_reason="too_few_frames", refusals=["too_few_frames"],
                  o_mag_m=0.104, frames_used=0, sam_model="sam3/3.1@abc",
                  sam_prompts=["slate", "white board"], sam_threshold=0.3,
                  render_long_side=1600, algorithm_version="1", code_version="test")  # fmt: skip
    fields.update(over)
    return ShadowCalibrationRow(**fields)


# -- the tables ------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_the_app_role_may_only_read_and_append(owner_engine, table):
    privileges = await _rows(owner_engine, """
        SELECT privilege_type FROM information_schema.role_table_grants
        WHERE table_name = :t AND grantee = 'fishsense_app' ORDER BY 1""", t=table)  # fmt: skip
    assert [p["privilege_type"] for p in privileges] == ["INSERT", "SELECT"]


async def test_nothing_in_production_reads_the_shadow_tables(owner_engine):
    """§3: no view or function depends on them but their own `current_` view,
    so no calibration, depth or measurement can ever read a shadow result."""
    dependents = await _rows(owner_engine, """
        SELECT DISTINCT dv.relname AS view
        FROM pg_depend d
        JOIN pg_rewrite r ON r.oid = d.objid
        JOIN pg_class dv ON dv.oid = r.ev_class
        JOIN pg_class t ON t.oid = d.refobjid
        WHERE t.relname = ANY(:tables) AND dv.relname <> t.relname
        """, tables=list(TABLES))  # fmt: skip
    assert [d["view"] for d in dependents] == ["current_shadow_laser_calibrations"]
    functions = await _rows(owner_engine, """
        SELECT proname FROM pg_proc
        WHERE prosrc ILIKE '%shadow_laser_calibrations%'
           OR prosrc ILIKE '%shadow_calibration_frames%'
        """)  # fmt: skip
    assert functions == []


async def test_a_tenant_sees_only_its_own_shadow_rows(owner_engine, app_engine):
    lab, other = await tenant(owner_engine, "lab"), await tenant(owner_engine, "other")
    m = await _mission(owner_engine, lab)
    await _in(app_engine, lab, persist_shadow_calibrations, m, [_row()])
    async with tenant_transaction(app_engine, other) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM shadow_laser_calibrations"))
        ).scalar_one() == 0


async def test_an_accepted_row_carries_its_beam_and_a_refused_one_its_reason(
    owner_engine, app_engine
):
    from sqlalchemy.exc import IntegrityError

    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    for bad in (_row(outcome="accepted", refusal_reason=None, refusals=[]),
                _row(refusal_reason=None)):  # fmt: skip
        with pytest.raises(IntegrityError):
            await _in(app_engine, lab, persist_shadow_calibrations, m, [bad])


# -- what it reads ----------------------------------------------------------------


async def test_slate_frames_are_labelled_or_detected_and_say_which(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    labelled, detected, both, neither, gone = [
        await capture(owner_engine, lab, m) for _ in range(5)
    ]
    for c in (labelled, both):
        await slate_label(owner_engine, lab, c)
    await slate_label(owner_engine, lab, gone, superseded=True)
    for c in (detected, both):
        await slate_presence(owner_engine, lab, c, probability=0.9)
    await slate_presence(owner_engine, lab, neither, probability=0.2)

    inputs = await _in(app_engine, lab, shadow_calibration_inputs, m,
                       headtail_version=HV, slate_frames=slate_frames)  # fmt: skip

    assert {f.capture_id: f.frame_source for f in inputs.frames} == {
        labelled: "label", detected: "detector", both: "both"}  # fmt: skip
    assert {f.capture_id: f.slate_probability for f in inputs.frames}[
        both
    ] == pytest.approx(0.9)


async def test_the_label_dot_is_productions_lowest_live_one_whatever_its_source(
    owner_engine, app_engine
):
    """§1.3: production keeps auto-accepted labels; so does the shadow stage,
    and it records the source."""
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    frame = await capture(owner_engine, lab, m)
    await slate_label(owner_engine, lab, frame)
    await laser_label(
        owner_engine, lab, frame, x=1.0, y=1.0, superseded=True, project=71
    )
    auto = await laser_label(owner_engine, lab, frame, x=2.0, y=3.0, project=72)
    await run(
        owner_engine,
        "UPDATE laser_labels SET source = 'auto_accept' WHERE id = :i",
        i=auto,
    )
    await laser_label(owner_engine, lab, frame, x=9.0, y=9.0, project=73)

    (f,) = (
        await _in(app_engine, lab, shadow_calibration_inputs, m, headtail_version=HV)
    ).frames

    assert (f.label_dot, f.label_id, f.label_source) == (
        (2.0, 3.0),
        auto,
        "auto_accept",
    )


async def test_the_label_dots_are_the_ones_productions_calibration_reads(
    owner_engine, app_engine
):
    from _slate_calibration_seed import slate_template

    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    m = await mission(owner_engine, lab, slate=await slate_template(owner_engine),
                      device=device)  # fmt: skip
    for k in range(3):
        c = await capture(owner_engine, lab, m)
        await slate_label(owner_engine, lab, c, completed=True)
        await laser_label(owner_engine, lab, c, x=10.0 + k, y=20.0 + k, project=71)
        await laser_label(owner_engine, lab, c, x=99.0, y=99.0, project=72)

    production = await _in(app_engine, lab, slate_calibration_inputs, m)
    shadow = await _in(
        app_engine, lab, shadow_calibration_inputs, m, headtail_version=HV
    )

    assert [(o.capture_id, (o.laser_x, o.laser_y)) for o in production.observations] == [
        (f.capture_id, f.label_dot) for f in shadow.frames]  # fmt: skip


async def test_detector_dots_are_the_automatic_tracks_at_its_version(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    slate, fish, stale = [await capture(owner_engine, lab, m) for _ in range(3)]
    await slate_label(owner_engine, lab, slate)
    pid = await _automatic_dot(owner_engine, lab, slate, 2000.0, 1500.0, 0.97)
    await _automatic_dot(owner_engine, lab, fish, 2010.0, 1450.0, 0.4)
    await _automatic_dot(owner_engine, lab, stale, 1.0, 1.0, 1.0, version=HV - 1)

    inputs = await _in(
        app_engine, lab, shadow_calibration_inputs, m, headtail_version=HV
    )

    (f,) = inputs.frames
    assert (f.detector_dot, f.detector_confidence, f.prediction_id) == (
        (2000.0, 1500.0),
        0.97,
        pid,
    )
    assert sorted(inputs.mission_detector_dots) == [
        (2000.0, 1500.0, 0.97),
        (2010.0, 1450.0, 0.4),
    ]


@pytest.mark.parametrize("borrowed", [False, True])
async def test_the_reference_is_productions_effective_calibration(
    owner_engine, app_engine, borrowed
):
    lab = await tenant(owner_engine)
    source = await _mission(owner_engine, lab)
    m = await _mission(owner_engine, lab, source=source if borrowed else None)
    cal = await laser_calibration(owner_engine, lab, source if borrowed else m)

    inputs = await _in(
        app_engine, lab, shadow_calibration_inputs, m, headtail_version=HV
    )

    assert (inputs.reference_laser_calibration_id, inputs.reference_borrowed) == (
        cal,
        borrowed,
    )
    assert len(inputs.reference_axis) == len(inputs.reference_position) == 3


async def test_the_mission_line_is_its_current_one(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    for a in (0.1, 0.2):
        await run(owner_engine, """
            INSERT INTO mission_laser_lines (tenant_id, mission_id, a, b, c, n_points,
                inlier_count, inlier_fraction, residual_std, label_noise_mad,
                line_confidence, noise_estimator)
            VALUES (:t, :d, :a, 0.9, -100, 10, 9, 0.9, 1, 1, 0.9, 'signed_residual_mad')
            """, t=lab, d=m, a=a)  # fmt: skip
    inputs = await _in(
        app_engine, lab, shadow_calibration_inputs, m, headtail_version=HV
    )
    assert inputs.mission_line == (0.2, 0.9, -100.0)


# -- writing ----------------------------------------------------------------------


async def test_a_result_and_its_frames_are_written_together(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    c = await capture(owner_engine, lab, m)
    (rid,) = await _in(app_engine, lab, persist_shadow_calibrations, m, [
        _row(outcome="accepted", refusal_reason=None, refusals=[],
             laser_position=[0.03, -0.1, 0.0], laser_axis=[0.0, 0.02, 1.0],
             frames_used=1, frames_rejected={"trimmed": 2}, gate={"off_line_px": 10.0},
             frames=[ShadowFrameRow(capture_id=c, frame_source="label", used=True,
                                    dot_x=1.0, dot_y=2.0, size=33.0)])])  # fmt: skip

    (row,) = await _rows(
        owner_engine, "SELECT * FROM current_shadow_laser_calibrations"
    )
    assert (row["id"], row["frames_rejected"], row["gate"]) == (
        rid,
        {"trimmed": 2},
        {"off_line_px": 10.0},
    )
    (frame,) = await _rows(owner_engine, "SELECT * FROM shadow_calibration_frames")
    assert (frame["shadow_laser_calibration_id"], frame["size"]) == (rid, 33.0)


async def test_a_frame_of_another_mission_is_refused(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m, other = await _mission(owner_engine, lab), await _mission(owner_engine, lab)
    c = await capture(owner_engine, lab, other)
    with pytest.raises(ValueError):
        await _in(app_engine, lab, persist_shadow_calibrations, m, [
            _row(frames=[ShadowFrameRow(capture_id=c, frame_source="label", used=False,
                                        rejected_reason="no_mask_at_dot")])])  # fmt: skip


# -- the cohort ---------------------------------------------------------------------


async def test_calibrated_missions_come_first_then_the_oldest(owner_engine, app_engine):
    from datetime import UTC, datetime

    lab = await tenant(owner_engine)
    old = await _mission(owner_engine, lab, created_at=datetime(2024, 1, 1, tzinfo=UTC))
    calibrated = await _mission(
        owner_engine, lab, created_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    await laser_calibration(owner_engine, lab, calibrated)
    bare = await _mission(owner_engine, lab)
    for m in (old, calibrated):
        await slate_label(owner_engine, lab, await capture(owner_engine, lab, m))
    await capture(owner_engine, lab, bare)  # no slate frame: never selected

    async def nxt(**kw):
        return await _in(app_engine, lab, next_mission_for_shadow_calibration,
                         algorithm_version="1", **kw)  # fmt: skip

    assert await nxt() == calibrated
    await _in(app_engine, lab, persist_shadow_calibrations, calibrated, [_row()])
    assert await nxt() == old
    assert await nxt(exclude=[old]) is None


# -- production is untouched --------------------------------------------------------


async def test_writing_shadow_results_changes_no_calibration_depth_or_measurement(
    owner_engine, app_engine
):
    """§9.1: dump every view production calibrates, depths and measures from,
    write shadow results (both sources, both variants) for the mission, and
    dump again: identical."""
    from depth_measure_seed import calibrated_dive, measurable_capture

    lab = await tenant(owner_engine)
    m, _ = await calibrated_dive(owner_engine, lab)
    c = await measurable_capture(owner_engine, lab, m)
    views = ("laser_calibrations", "current_laser_calibrations",
             "effective_laser_calibrations", "mission_laser_geometry",
             "laser_depth_work", "measurement_work", "current_measurements",
             "automatic_measurement_calibrations", "current_automatic_measurements",
             "current_mission_laser_lines")  # fmt: skip

    async def dump():
        out = {}
        for v in views:
            out[v] = await _rows(owner_engine, f"SELECT * FROM {v} ORDER BY 1::text")
        return out

    before = await dump()
    await _in(app_engine, lab, persist_shadow_calibrations, m, [
        _row(dot_source=s, variant=v, outcome="accepted", refusal_reason=None, refusals=[],
             laser_position=[0.03, -0.1, 0.0], laser_axis=[0.0, 0.02, 1.0], frames_used=1,
             frames=[ShadowFrameRow(capture_id=c, frame_source="both", used=True)])
        for s in ("labels", "detector") for v in ("port", "gated")])  # fmt: skip
    assert await dump() == before
