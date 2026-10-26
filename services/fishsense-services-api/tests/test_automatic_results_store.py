"""The automatic-results track's database side, on real Postgres.

New in v2 (no v1 counterpart): fish lengths with no human label, by the chain
cscw-fishsense2027@96a8da07 validated (PAPER.md §6): the production laser
detector's dot, a SAM 3.1 mask seeded at that dot (kept only if the dot is on
it, score >= 0.5), a geometric head/tail, a label-free size-constancy
calibration per dive, a length. Decided 2026-10-06, and pinned here:

* **a separate track**: automatic results are their own append-only tables
  (producer "automatic"), tenant-scoped under forced RLS; nothing here writes a
  label, a human-path prediction or a `measurements` row, and nothing automatic
  is ever in `current_measurements` or `measurement_work`;
* **the backlog cohort**: dives of any priority with no human measurement,
  oldest first, renderable, with automatic work outstanding; it drains --
  abstentions, refusals and refused calibrations all count as done;
* **current** (PLAN.md §9.13): the latest automatic length per capture whose
  inputs still hold -- its head/tail is the capture's current automatic one,
  its calibration the dive's current measurement calibration;
* **the calibration a length uses**: the dive's own accepted label-free fit,
  else its calibration link's, else its effective stored calibration --
  recorded as `label_free` or `stored`;
* **slate frames** (a detector being built in parallel) come through a small
  interface, stubbed to none: at p >= 0.5 a frame is not measured as a fish,
  and is a candidate for the label-free fit;
* the processor's output is checked: rows for another dive's captures, or
  naming another capture's head/tail, are refused and nothing is written.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    restate_port,
    PLAUSIBLE_POSITION,
    calibrate,
    calibrated_dive,
    capture,
    device,
    mission,
    exec_,
    fish,
    forget_identities,
    head_tail_label,
    measurable_capture,
    measurement,
    tenant,
)
from fishsense_services_api.automatic_results_store import (
    AUTOMATIC_CALIBRATION_CAPTURES,
    AUTOMATIC_MEASURE_CAPTURES,
    AUTOMATIC_SPECIES_CAPTURES,
    SLATE_FRAME_THRESHOLD,
    AutomaticCalibrationRow,
    AutomaticHeadTailRow,
    AutomaticMeasurementRow,
    AutomaticResultsCatalog,
    AutomaticSpeciesRow,
    ForeignCapture,
    ForeignHeadTail,
    automatic_calibration_inputs,
    automatic_frames_inputs,
    automatic_measure_inputs,
    automatic_results_cohort,
    AUTOMATIC_FRAME_MAX_FAILURES,
    record_automatic_frame_failures,
    record_automatic_results_failure,
    automatic_species_captures,
    next_mission_for_automatic_results,
    no_slate_frames,
    persist_automatic_calibration,
    persist_automatic_head_tails,
    persist_automatic_measurements,
    persist_automatic_species,
    _pinhole_camera,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.prediction_masks_store import PredictionMask

VERSIONS = {"headtail_version": 1, "species_version": 1,
            "calibration_version": "1", "measurement_version": "1"}  # fmt: skip
ORCHESTRATOR = "service:fishsense-orchestrator"
BOX = [100, 200, 300, 260]
HOGFISH = "Fish, Hogfish (Lachnolaimus maximus)"


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _rows(engine, sql, **params):
    async with engine.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql), params)]


async def _backlog_mission(owner_engine, lab, *, priority="low", **kwargs):
    device_id = await device(owner_engine, lab)
    return await mission(
        owner_engine, lab, device_id=device_id, priority=priority, **kwargs
    )


async def _next_without_species(app_engine, lab):
    """The cohort's next dive with species off: a rendered fish owes species
    too, which these tests are not about."""
    candidate = await _in(app_engine, lab, next_mission_for_automatic_results,
                          **VERSIONS, species=False)  # fmt: skip
    return None if candidate is None else candidate.mission_id


async def _next(app_engine, lab):
    candidate = await _in(
        app_engine, lab, next_mission_for_automatic_results, **VERSIONS
    )
    return None if candidate is None else candidate.mission_id


def _predicted(capture_id, **extra) -> AutomaticHeadTailRow:
    fields = dict(
        capture_id=capture_id, status="predicted", laser_x=2000.0, laser_y=1500.0,
        laser_confidence=0.9, laser_predictor_version=3, laser_checkpoint="laser@x",
        head_x=1800.0, head_y=1500.0, tail_x=2300.0, tail_y=1480.0, width=4000,
        height=3000, mask_area_px=50_000, silhouette_ratio=0.3, crop_x=1100,
        crop_y=825, mask_bbox=BOX, sam_score=0.83, predictor_version=1,
        checkpoint="sam3/3.1@abc", core_version="4.1.0",
    )  # fmt: skip
    fields.update(extra)
    return AutomaticHeadTailRow(**fields)


async def _camera(app_engine, lab, mission_id) -> uuid.UUID:
    """The mission's pinhole camera calibration: what its raws render with."""
    return (await _in(app_engine, lab, _pinhole_camera, mission_id)).id


#: Statuses with no render (the raw was not there, or would not decode).
_NO_RENDER = {"raw_unavailable", "decode_failed"}


async def _predict(app_engine, lab, mission_id, *rows, rendered=True, keypoints=True):
    """Append rows; `rendered`, each frame with a raw names render 1 under the
    mission's camera, as the frames stage writes them (0049). `keypoints`:
    each rendered fish also gets SimCC keypoints that agree with its SAM rule
    (head/tail v5), so it is ready to measure as v5 leaves a frame."""
    if rendered:
        camera = await _camera(app_engine, lab, mission_id)
        rows = [
            r if r.status in _NO_RENDER or r.jpeg_render_version is not None
            else replace(r, jpeg_render_version=1, jpeg_camera_calibration_id=camera)
            for r in rows
        ]  # fmt: skip
    written = await _in(
        app_engine, lab, persist_automatic_head_tails, mission_id, list(rows)
    )
    if keypoints:
        from fishsense_services_api.automatic_results_store import (
            AutomaticKeypointRow,
            automatic_keypoint_inputs,
            persist_automatic_keypoints,
        )

        mine = {r.capture_id for r in rows}
        heads = {r.capture_id: r for r in rows}
        owed = [w for w in await _in(app_engine, lab, automatic_keypoint_inputs,
                                     mission_id) if w.capture_id in mine]  # fmt: skip
        await _in(app_engine, lab, persist_automatic_keypoints, mission_id, [
            AutomaticKeypointRow(
                prediction_id=w.prediction_id, capture_id=w.capture_id,
                status="predicted",
                head=[heads[w.capture_id].head_x, heads[w.capture_id].head_y],
                tail=[heads[w.capture_id].tail_x, heads[w.capture_id].tail_y],
                confidence=0.5, checkpoint="simcc@test")
            for w in owed
        ])  # fmt: skip
    return written


def _label_free(**extra) -> AutomaticCalibrationRow:
    fields = dict(
        outcome="accepted", algorithm_version="1", laser_position=[0.03, -0.1, 0.0],
        laser_axis=[0.01, 0.03, 0.9995], vanishing_px=-2000.0,
        line_direction=[-0.29, -0.957], line_offset_px=1500.0, o_mag_m=0.104,
        frames_used=12, pair_count=40, size_ratio=2.1, se_px=0.9,
        pair_residual_sd=0.01, capture_ids=[], core_version="4.1.0",
    )  # fmt: skip
    fields.update(extra)
    return AutomaticCalibrationRow(**fields)


# -- the tables: append-only, tenant-scoped, producer "automatic" --------------------

TABLES = (
    "automatic_head_tail_predictions",
    "automatic_laser_calibrations",
    "automatic_species_predictions",
    "automatic_measurements",
    "prediction_masks",
)


@pytest.mark.parametrize("table", TABLES)
async def test_the_app_role_may_only_read_and_append(owner_engine, table):
    privileges = await _rows(
        owner_engine,
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE table_name = :t AND grantee = 'fishsense_app' ORDER BY 1",
        t=table,
    )
    assert [p["privilege_type"] for p in privileges] == ["INSERT", "SELECT"]


async def test_rows_are_the_tenants_own(owner_engine, app_engine):
    lab, partner = await tenant(owner_engine, "lab"), await tenant(owner_engine, "p")
    mission_id = await _backlog_mission(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(capture_id))

    async with tenant_transaction(app_engine, partner) as conn:
        seen = (
            await conn.execute(
                text("SELECT count(*) FROM automatic_head_tail_predictions")
            )
        ).scalar_one()
    assert seen == 0


async def test_every_row_is_produced_automatic(owner_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, mission_id)
    with pytest.raises(IntegrityError, match="producer"):
        await exec_(
            owner_engine,
            "INSERT INTO automatic_head_tail_predictions (tenant_id, capture_id, "
            "status, predictor_version, producer) VALUES (:t, :c, 'no_detections', "
            "1, 'human')",
            t=lab,
            c=capture_id,
        )


async def test_a_prediction_below_the_sam_gate_is_not_a_prediction(
    owner_engine, app_engine
):
    """Paper §6.3: SAM's own confidence >= 0.5 is the best mask gate."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, mission_id)
    with pytest.raises(DBAPIError, match="sam"):
        await _predict(
            app_engine, lab, mission_id, _predicted(capture_id, sam_score=0.49)
        )


async def test_a_prediction_with_no_sam_score_is_not_a_prediction(
    owner_engine, app_engine
):
    """`sam_score >= 0.5` is NULL, not false, for a NULL score, and a CHECK
    passes NULL: an unscored mask was a prediction (2026-10-15 gap audit)."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, mission_id)
    with pytest.raises(DBAPIError, match="sam"):
        await _predict(
            app_engine, lab, mission_id, _predicted(capture_id, sam_score=None)
        )


# -- the backlog cohort -------------------------------------------------------------


@pytest.mark.parametrize("priority", ["low", "none", "high"])
async def test_the_cohort_is_any_priority(owner_engine, app_engine, priority):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab, priority=priority)
    await capture(owner_engine, lab, mission_id)

    assert await _next(app_engine, lab) == mission_id


async def test_a_mission_with_no_capture_has_no_work(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _backlog_mission(owner_engine, lab)
    assert await _next(app_engine, lab) is None


async def test_a_mission_with_a_human_measurement_is_selected_too(
    owner_engine, app_engine
):
    """The owner, 2026-10-24: human-measured missions are where automatic
    results are checked against people, so they are kept current like any
    other (until then they were skipped, and a cut-mask fix at version 4
    never reached them)."""
    lab = await tenant(owner_engine)
    measured, cal = await calibrated_dive(owner_engine, lab, priority="low")
    c = await measurable_capture(owner_engine, lab, measured)
    await measurement(owner_engine, lab, c, await fish(owner_engine, lab), cal)

    assert await _next(app_engine, lab) == measured


async def test_a_mission_without_a_human_measurement_goes_first(
    owner_engine, app_engine
):
    """The backlog -- missions nobody has measured -- still comes first, even
    behind a human-measured mission that is older."""
    lab = await tenant(owner_engine)
    measured, cal = await calibrated_dive(owner_engine, lab, priority="low")
    c = await measurable_capture(owner_engine, lab, measured)
    await measurement(owner_engine, lab, c, await fish(owner_engine, lab), cal)
    backlog = await _backlog_mission(owner_engine, lab)
    await capture(owner_engine, lab, backlog)
    await exec_(owner_engine, "UPDATE missions SET created_at = created_at + "
                "interval '1 day' WHERE id = :d", d=backlog)  # fmt: skip

    assert await _next(app_engine, lab) == backlog


async def test_oldest_first(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    newer = await _backlog_mission(owner_engine, lab)
    older = await _backlog_mission(owner_engine, lab)
    await exec_(owner_engine, "UPDATE missions SET created_at = created_at - "
                "interval '1 day' WHERE id = :d", d=older)  # fmt: skip
    for d in (newer, older):
        await capture(owner_engine, lab, d)

    assert await _next(app_engine, lab) == older


async def test_a_mission_the_pipeline_cannot_rectify_is_not_selected(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    uncalibrated = await device(owner_engine, lab, camera_matrix=False)
    d = await mission(owner_engine, lab, device_id=uncalibrated, priority="low")
    await capture(owner_engine, lab, d)
    none = await mission(owner_engine, lab, priority="low")
    await capture(owner_engine, lab, none)

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize("port", ["unknown", "flat", "dome"])
async def test_a_mission_not_behind_the_corrective_optic_is_not_selected(
    owner_engine, app_engine, port
):
    """2b-2: the frames stage rectifies the raw as a pinhole camera."""
    lab = await tenant(owner_engine)
    d = await _backlog_mission(owner_engine, lab)
    await capture(owner_engine, lab, d)
    assert await _next(app_engine, lab) == d

    await restate_port(owner_engine, d, port)

    assert await _next(app_engine, lab) is None


async def test_the_cohort_drains(owner_engine, app_engine):
    """Every frame predicted (or abstained), a calibration row (even a
    refusal), no length possible without a calibration: done."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish_frame = await capture(owner_engine, lab, mission_id)
    empty_frame = await capture(owner_engine, lab, mission_id)
    await capture(owner_engine, lab, mission_id, canonical=False)
    assert await _next(app_engine, lab) == mission_id

    await _predict(
        app_engine, lab, mission_id, _predicted(fish_frame),
        AutomaticHeadTailRow(capture_id=empty_frame, status="no_laser_dot",
                             predictor_version=1),
    )  # fmt: skip
    assert await _next(app_engine, lab) == mission_id  # species, calibration left

    (job,) = await _in(app_engine, lab, automatic_species_captures, mission_id,
                       species_version=1)  # fmt: skip
    await _in(app_engine, lab, persist_automatic_species, mission_id, [
        AutomaticSpeciesRow(capture_id=fish_frame,
                            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
                            status="decode_failed", predictor_version=1, model_id="m")
    ])  # fmt: skip
    assert await _next(app_engine, lab) == mission_id  # calibration left

    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip
    # Refused and no stored calibration: no length is possible, so no work.
    assert await _next(app_engine, lab) is None


async def test_with_species_off_species_is_no_work(owner_engine, app_engine):
    """Species has its own switch (2026-10-15): off, a dive whose only work
    left is species leaves the cohort, so it never holds the backlog."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish_frame = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(fish_frame))
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip

    assert await _next(app_engine, lab) == mission_id  # species outstanding
    off = await _in(app_engine, lab, next_mission_for_automatic_results,
                    species=False, **VERSIONS)  # fmt: skip
    assert off is None


async def test_a_new_version_reopens_the_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c, predictor_version=0))

    inputs = await _in(app_engine, lab, automatic_frames_inputs, mission_id,
                       headtail_version=1)  # fmt: skip
    assert [f.capture_id for f in inputs.frames] == [c]


# Version 4 (2026-10-22) changed only a frame whose mask ran into an edge of the
# 1800x1350 window that is not the photo's: it is segmented again on the whole
# photo. Any other version-3 row is what 4 would write, so it stands; the cut
# ones are owed, and the drain re-runs exactly those.

#: A window 1800x1350 at (1100, 825) in a 4000x3000 frame.
_WINDOW = dict(crop_x=1100, crop_y=825, width=4000, height=3000)


async def _owed_at_4(app_engine, lab, mission_id):
    inputs = await _in(app_engine, lab, automatic_frames_inputs, mission_id,
                       headtail_version=4)  # fmt: skip
    return [f.capture_id for f in inputs.frames]


async def test_at_4_a_version_3_frame_inside_the_window_stands(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(
        c, predictor_version=3, mask_bbox=[1101, 826, 2899, 2174], **_WINDOW))  # fmt: skip

    assert await _owed_at_4(app_engine, lab, mission_id) == []


@pytest.mark.parametrize(
    "box",
    [[1100, 900, 2000, 1000],   # the window's left edge
     [2000, 900, 2900, 1000],   # its right (half-open: 1100 + 1800)
     [2000, 825, 2100, 1000],   # its top
     [2000, 1000, 2100, 2175]],  # its bottom (825 + 1350)
)  # fmt: skip
async def test_at_4_a_version_3_frame_cut_by_the_window_is_owed(
    owner_engine, app_engine, box
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   _predicted(c, predictor_version=3, mask_bbox=box, **_WINDOW))  # fmt: skip

    assert await _owed_at_4(app_engine, lab, mission_id) == [c]


async def test_at_4_a_version_3_frame_cut_only_by_the_photos_edge_stands(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(
        c, predictor_version=3, mask_bbox=[0, 900, 600, 1000],
        crop_x=0, crop_y=825, width=4000, height=3000))  # fmt: skip

    assert await _owed_at_4(app_engine, lab, mission_id) == []


async def test_at_4_a_version_3_cut_mask_that_failed_keypointing_is_owed(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(
        c, predictor_version=3, status="headtail_failed", head_x=None, head_y=None,
        tail_x=None, tail_y=None, silhouette_ratio=None,
        mask_bbox=[1100, 900, 2900, 1000], **_WINDOW))  # fmt: skip

    assert await _owed_at_4(app_engine, lab, mission_id) == [c]


async def test_at_4_version_3_abstentions_stand_but_an_unscored_mask_is_owed(
    owner_engine, app_engine
):
    """An unscored mask was kept with no box recorded, so whether the window
    cut it is unknown: it is run again."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    no_dot = await capture(owner_engine, lab, mission_id)
    unscored = await capture(owner_engine, lab, mission_id)
    bare = dict(head_x=None, head_y=None, tail_x=None, tail_y=None, mask_bbox=None,
                silhouette_ratio=None, mask_area_px=None, sam_score=None,
                crop_x=None, crop_y=None, predictor_version=3)  # fmt: skip
    await _predict(app_engine, lab, mission_id,
                   _predicted(no_dot, status="no_laser_dot", laser_x=None,
                              laser_y=None, laser_confidence=None, **bare),
                   _predicted(unscored, status="mask_unscored", **bare))  # fmt: skip

    assert await _owed_at_4(app_engine, lab, mission_id) == [unscored]


async def test_at_4_a_version_2_frame_is_still_owed(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(
        c, predictor_version=2, mask_bbox=[1101, 826, 2899, 2174], **_WINDOW))  # fmt: skip

    assert await _owed_at_4(app_engine, lab, mission_id) == [c]


async def test_new_frames_make_the_calibration_stale(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=a, status="no_laser_dot",
                                        predictor_version=1))  # fmt: skip
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip
    assert await _next(app_engine, lab) is None

    b = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=b, status="slate_frame",
                                        laser_x=1.0, laser_y=2.0,
                                        predictor_version=1))  # fmt: skip
    assert await _next(app_engine, lab) == mission_id


# -- frames: what the GPU stage runs on ----------------------------------------------


async def test_frames_are_the_canonical_captures_with_their_intrinsics(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    await capture(owner_engine, lab, mission_id, canonical=False)

    inputs = await _in(app_engine, lab, automatic_frames_inputs, mission_id,
                       headtail_version=1)  # fmt: skip

    assert [f.capture_id for f in inputs.frames] == [a]
    assert inputs.frames[0].slate_probability is None
    assert inputs.camera_matrix[0][0] == 3000.0
    assert len(inputs.distortion_coefficients) == 5


async def test_slate_frames_ride_on_the_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    slate = await capture(owner_engine, lab, mission_id)
    fish_frame = await capture(owner_engine, lab, mission_id)

    async def detector(conn, tenant_id, d):
        assert (tenant_id, d) == (lab, mission_id)
        return [(slate, 0.97), (fish_frame, 0.2)]

    inputs = await _in(app_engine, lab, automatic_frames_inputs, mission_id,
                       headtail_version=1, slate_frames=detector)  # fmt: skip

    by = {f.capture_id: f for f in inputs.frames}
    assert by[slate].is_slate and by[slate].slate_probability == 0.97
    assert not by[fish_frame].is_slate
    assert SLATE_FRAME_THRESHOLD == 0.5


async def test_the_slate_interface_is_stubbed_to_none(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    assert await _in(app_engine, lab, no_slate_frames, mission_id) == []


# -- persisting the GPU stage's output ---------------------------------------------


async def test_rows_for_another_mission_are_refused_and_nothing_is_written(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    other = await _backlog_mission(owner_engine, lab)
    mine = await capture(owner_engine, lab, mission_id)
    theirs = await capture(owner_engine, lab, other)

    with pytest.raises(ForeignCapture):
        await _predict(
            app_engine, lab, mission_id, _predicted(mine), _predicted(theirs)
        )
    assert (
        await _rows(owner_engine, "SELECT 1 FROM automatic_head_tail_predictions") == []
    )


async def test_the_human_path_is_untouched(owner_engine, app_engine):
    """No label, no human-path prediction, no measurement."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))

    for table in ("laser_labels", "head_tail_labels", "species_labels",
                  "laser_predictions", "head_tail_predictions",
                  "species_predictions", "measurements", "laser_calibrations"):  # fmt: skip
        assert await _rows(owner_engine, f"SELECT 1 FROM {table}") == [], table


# -- species: BioCLIP zero-shot on the automatic mask ------------------------------


async def test_species_jobs_are_the_predicted_masks(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a, b = await capture(owner_engine, lab, mission_id), await capture(
        owner_engine, lab, mission_id
    )
    await _predict(app_engine, lab, mission_id, _predicted(a),
                   AutomaticHeadTailRow(capture_id=b, status="no_detections",
                                        predictor_version=1))  # fmt: skip

    (job,) = await _in(app_engine, lab, automatic_species_captures, mission_id,
                       species_version=1)  # fmt: skip

    assert job.capture_id == a and job.mask_bbox == BOX


async def test_species_naming_another_captures_mask_is_refused(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a, b = await capture(owner_engine, lab, mission_id), await capture(
        owner_engine, lab, mission_id
    )
    await _predict(app_engine, lab, mission_id, _predicted(a), _predicted(b))
    jobs = await _in(app_engine, lab, automatic_species_captures, mission_id,
                     species_version=1)  # fmt: skip
    by = {j.capture_id: j.automatic_head_tail_prediction_id for j in jobs}

    with pytest.raises(ForeignHeadTail):
        await _in(app_engine, lab, persist_automatic_species, mission_id, [
            AutomaticSpeciesRow(capture_id=a, automatic_head_tail_prediction_id=by[b],
                                status="decode_failed", predictor_version=1,
                                model_id="m")])  # fmt: skip


# -- the automatic render ------------------------------------------------------------


async def test_a_frames_render_reaches_species_and_calibration(
    owner_engine, app_engine
):
    """The frames stage records which render of the raw its dot and mask were
    found in -- the render version and the camera calibration it rectified
    with -- and species and calibration read that render (2026-10-19). A row
    from before renders were recorded is no species work: its pixels are in
    no automatic render."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    camera = await _camera(app_engine, lab, mission_id)
    fish = await capture(owner_engine, lab, mission_id)
    slate = await capture(owner_engine, lab, mission_id)
    legacy = await capture(owner_engine, lab, mission_id)
    await _predict(
        app_engine, lab, mission_id, _predicted(fish),
        AutomaticHeadTailRow(capture_id=slate, status="slate_frame", laser_x=10.0,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1),
    )  # fmt: skip
    await _predict(app_engine, lab, mission_id, _predicted(legacy), rendered=False)

    async def detector(conn, tenant_id, d):
        return [(slate, 0.9)]

    jobs = await _in(app_engine, lab, automatic_species_captures, mission_id,
                     species_version=1)  # fmt: skip
    inputs = await _in(app_engine, lab, automatic_calibration_inputs, mission_id,
                       slate_frames=detector)  # fmt: skip
    frames = await _in(app_engine, lab, automatic_frames_inputs, mission_id,
                       headtail_version=2)  # fmt: skip

    assert [(j.capture_id, j.jpeg_render_version, j.jpeg_camera_calibration_id)
            for j in jobs] == [(fish, 1, camera)]  # fmt: skip
    assert [(c.capture_id, c.jpeg_render_version, c.jpeg_camera_calibration_id)
            for c in inputs.candidates] == [(slate, 1, camera)]  # fmt: skip
    assert inputs.camera_calibration_id == frames.camera_calibration_id == camera


async def test_an_unrendered_frame_is_no_species_work(owner_engine, app_engine):
    """It would hold its mission in the cohort with nothing able to do it."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish_frame = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(fish_frame), rendered=False)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize(
    "version, camera",
    [(0, True), (1, False), (None, True)],
    ids=["version-0", "version-without-camera", "camera-without-version"],
)
async def test_a_render_is_a_positive_version_and_a_camera(
    owner_engine, app_engine, version, camera
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    cam = await _camera(app_engine, lab, mission_id) if camera else None
    c = await capture(owner_engine, lab, mission_id)
    with pytest.raises(IntegrityError):
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO automatic_head_tail_predictions (tenant_id, "
                     "capture_id, status, predictor_version, jpeg_render_version, "
                     "jpeg_camera_calibration_id) "
                     "VALUES (:t, :c, 'no_laser_dot', 1, :v, :cam)"),
                {"t": lab, "c": c, "v": version, "cam": cam},
            )  # fmt: skip


async def test_a_render_names_one_of_the_tenants_cameras(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    with pytest.raises(IntegrityError):
        await _predict(app_engine, lab, mission_id, AutomaticHeadTailRow(
            capture_id=c, status="no_laser_dot", predictor_version=1,
            jpeg_render_version=1, jpeg_camera_calibration_id=uuid.uuid4()))  # fmt: skip


# -- calibration ---------------------------------------------------------------------


async def test_calibration_candidates_are_slate_frames_with_a_dot(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    on = await capture(owner_engine, lab, mission_id)
    nodot = await capture(owner_engine, lab, mission_id)
    fish_frame = await capture(owner_engine, lab, mission_id)
    await _predict(
        app_engine, lab, mission_id,
        AutomaticHeadTailRow(capture_id=on, status="slate_frame", laser_x=10.0,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1),
        AutomaticHeadTailRow(capture_id=nodot, status="no_laser_dot",
                             slate_probability=0.9, predictor_version=1),
        _predicted(fish_frame),
    )  # fmt: skip

    async def detector(conn, tenant_id, d):
        return [(on, 0.9), (nodot, 0.9), (fish_frame, 0.1)]

    inputs = await _in(app_engine, lab, automatic_calibration_inputs, mission_id,
                       slate_frames=detector)  # fmt: skip

    assert [(c.capture_id, c.x, c.y) for c in inputs.candidates] == [(on, 10.0, 20.0)]
    # The dive line is fitted from every automatic dot of the dive.
    assert sorted(map(tuple, inputs.line_dots)) == [(10.0, 20.0), (2000.0, 1500.0)]
    assert inputs.camera_matrix[0][0] == 3000.0


async def _measure_inputs(app_engine, lab, mission_id):
    return await _in(app_engine, lab, automatic_measure_inputs, mission_id,
                     algorithm_version="1")  # fmt: skip


async def test_lengths_use_the_missions_own_label_free_calibration_first(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await calibrate(owner_engine, lab, mission_id)  # a stored one, too
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    cal = await _in(
        app_engine, lab, persist_automatic_calibration, mission_id, _label_free()
    )

    inputs = await _measure_inputs(app_engine, lab, mission_id)

    assert inputs.calibration.source == "label_free"
    assert inputs.calibration.automatic_laser_calibration_id == cal
    assert inputs.calibration.laser_calibration_id is None
    assert [m.capture_id for m in inputs.captures] == [c]


async def test_then_the_calibration_links_label_free_one(owner_engine, app_engine):
    """Production's pairing: a fish dive borrows its calibration session's."""
    lab = await tenant(owner_engine)
    session = await _backlog_mission(owner_engine, lab)
    cal = await _in(
        app_engine, lab, persist_automatic_calibration, session, _label_free()
    )
    mission_id = await _backlog_mission(owner_engine, lab, source_mission=session)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))

    inputs = await _measure_inputs(app_engine, lab, mission_id)

    assert inputs.calibration.source == "label_free"
    assert inputs.calibration.automatic_laser_calibration_id == cal


async def test_a_missions_own_label_free_fit_beats_its_links(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    session = await _backlog_mission(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, session, _label_free())
    mission_id = await _backlog_mission(owner_engine, lab, source_mission=session)
    own = await _in(
        app_engine, lab, persist_automatic_calibration, mission_id, _label_free()
    )
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))

    inputs = await _measure_inputs(app_engine, lab, mission_id)

    assert inputs.calibration.automatic_laser_calibration_id == own


async def test_a_stored_calibration_is_never_used(owner_engine, app_engine):
    """Automatic lengths rest on the automatic stack alone (owner, 2026-10-22):
    a mission whose label-free fit was refused, with none to borrow through
    its link, is not measured -- production's stored calibration is not a
    fallback (0064; 0036 fell back to it)."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await calibrate(owner_engine, lab, mission_id, axis=(0.0, 0.0, 1.0))
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="too_few_frames",
                          laser_position=None, laser_axis=None))  # fmt: skip

    inputs = await _measure_inputs(app_engine, lab, mission_id)

    assert inputs.calibration is None and inputs.captures == []


async def test_slate_frames_are_never_measured(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=c, status="slate_frame",
                                        laser_x=1.0, laser_y=2.0,
                                        predictor_version=1))  # fmt: skip
    assert (await _measure_inputs(app_engine, lab, mission_id)).captures == []


async def test_a_frame_the_detector_now_calls_a_slate_is_not_measured(
    owner_engine, app_engine
):
    """Frames segmented before the detector landed were taken as fish; once it
    names one a slate, its mask is not measured (a version bump re-runs it)."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))

    async def detector(conn, tenant_id, d):
        return [(c, 0.8)]

    inputs = await _in(app_engine, lab, automatic_measure_inputs, mission_id,
                       algorithm_version="1", slate_frames=detector)  # fmt: skip
    assert inputs.captures == []


async def test_a_zoomed_frame_is_not_measured(owner_engine, app_engine):
    """The zoom gate (2026-10-25): a zoomed frame's depth and length assume
    intrinsics it was not shot at. 152 automatic lengths were on zoomed
    frames when the focal lengths were recorded."""
    from _slate_calibration_seed import zoom

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    wide, zoomed = [await capture(owner_engine, lab, mission_id) for _ in range(2)]
    await _predict(app_engine, lab, mission_id, _predicted(wide), _predicted(zoomed))
    await zoom(owner_engine, zoomed, 18.0)

    inputs = await _measure_inputs(app_engine, lab, mission_id)

    assert [m.capture_id for m in inputs.captures] == [wide]


async def test_a_zoomed_frame_does_not_hold_its_mission_in_the_cohort(
    owner_engine, app_engine
):
    """Never measured, so never a length outstanding -- else its mission is
    picked every hour (as a detector slate would be). Species is off here:
    it does not depend on the zoom, and stays owed."""
    from _slate_calibration_seed import zoom

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    # After the prediction, so the calibration is current.
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    assert await _next_without_species(app_engine, lab) == mission_id  # length owed

    await zoom(owner_engine, c, 14.62)

    assert await _next_without_species(app_engine, lab) is None


async def test_a_zoomed_frame_is_no_calibration_dot(owner_engine, app_engine):
    """Neither a slate candidate nor a dot on the dive line: its dot sits on
    another line (missions 374, 384, 386). Covers the live label-free fit and
    the v3/v4 replays, which read the same inputs."""
    from _slate_calibration_seed import zoom

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _camera(app_engine, lab, mission_id)
    frames = [await capture(owner_engine, lab, mission_id) for _ in range(3)]
    await _predict(app_engine, lab, mission_id, *[
        AutomaticHeadTailRow(capture_id=f, status="slate_frame", laser_x=10.0 + i,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1)
        for i, f in enumerate(frames)
    ])  # fmt: skip
    await zoom(owner_engine, frames[1], 18.0)
    await zoom(owner_engine, frames[2], 4.5)

    async def detector(conn, tenant_id, d):
        return [(f, 0.9) for f in frames]

    inputs = await _in(app_engine, lab, automatic_calibration_inputs, mission_id,
                       slate_frames=detector)  # fmt: skip

    assert [c.capture_id for c in inputs.candidates] == [frames[0], frames[2]]
    assert sorted(x for x, _ in inputs.line_dots) == [10.0, 12.0]


async def test_a_calibration_fitted_through_a_zoomed_frame_is_owed_again(
    owner_engine, app_engine
):
    """The zoom gate came after some fits (2026-10-25: mission 237's used 2
    zoomed frames of 18). Such a fit is stale: refitting it leaves the zoomed
    frames out. A zoomed frame the fit did not use leaves it current."""
    from _slate_calibration_seed import zoom

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    used, unused = [await capture(owner_engine, lab, mission_id) for _ in range(2)]
    await _predict(app_engine, lab, mission_id, *[
        AutomaticHeadTailRow(capture_id=c, status="slate_frame", laser_x=10.0,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1)
        for c in (used, unused)
    ])  # fmt: skip
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(capture_ids=[used]))  # fmt: skip
    assert await _next(app_engine, lab) is None

    await zoom(owner_engine, unused, 18.0)
    assert await _next(app_engine, lab) is None

    await zoom(owner_engine, used, 5.05)
    assert await _next(app_engine, lab) == mission_id


async def test_a_frame_after_the_laser_moved_is_not_measured_nor_owed(
    owner_engine, app_engine
):
    """The partial run (owner, 2026-10-25): the part of the dive before the
    move is measured; the rest is refused, and never holds the mission."""
    from _slate_calibration_seed import beam_moved_after

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    first, moved = [await capture(owner_engine, lab, mission_id) for _ in range(2)]
    await _predict(app_engine, lab, mission_id, _predicted(first), _predicted(moved))
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    await beam_moved_after(owner_engine, first)

    inputs = await _measure_inputs(app_engine, lab, mission_id)
    assert [m.capture_id for m in inputs.captures] == [first]

    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=first,
            automatic_head_tail_prediction_id=inputs.captures[0].automatic_head_tail_prediction_id,
            automatic_keypoint_prediction_id=inputs.captures[0].automatic_keypoint_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            laser_calibration_id=inputs.calibration.laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
            length_m=0.42, depth_m=2.1, algorithm="laser_depth_fronto_parallel",
            algorithm_version="1", core_version="4.1.0",
        )
    ])  # fmt: skip
    assert (
        await _next_without_species(app_engine, lab) is None
    )  # the moved frame owes nothing


async def test_a_frame_after_the_laser_moved_is_no_calibration_dot(
    owner_engine, app_engine
):
    from _slate_calibration_seed import beam_moved_after

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _camera(app_engine, lab, mission_id)
    frames = [await capture(owner_engine, lab, mission_id) for _ in range(2)]
    await _predict(app_engine, lab, mission_id, *[
        AutomaticHeadTailRow(capture_id=f, status="slate_frame", laser_x=10.0 + i,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1)
        for i, f in enumerate(frames)
    ])  # fmt: skip
    await beam_moved_after(owner_engine, frames[0])

    async def detector(conn, tenant_id, d):
        return [(f, 0.9) for f in frames]

    inputs = await _in(app_engine, lab, automatic_calibration_inputs, mission_id,
                       slate_frames=detector)  # fmt: skip

    assert [c.capture_id for c in inputs.candidates] == [frames[0]]
    assert [x for x, _ in inputs.line_dots] == [10.0]


async def test_a_calibration_fitted_across_the_move_is_owed_again(
    owner_engine, app_engine
):
    """Fitted before the move was recorded, through frames of both aims: refit
    on the first part."""
    from _slate_calibration_seed import beam_moved_after

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    first, moved = [await capture(owner_engine, lab, mission_id) for _ in range(2)]
    await _predict(app_engine, lab, mission_id, *[
        AutomaticHeadTailRow(capture_id=c, status="slate_frame", laser_x=10.0,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1)
        for c in (first, moved)
    ])  # fmt: skip
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(capture_ids=[first, moved]))  # fmt: skip
    assert await _next(app_engine, lab) is None

    await beam_moved_after(owner_engine, first)

    assert await _next(app_engine, lab) == mission_id


# -- measurements: current, stale, never the human path's -------------------------


async def _measured(owner_engine, app_engine, lab, **predicted):
    """A mission with one current automatic length; ``predicted`` overrides
    the frame's prediction (`_predicted`)."""
    mission_id = await _backlog_mission(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c, **predicted))
    inputs = await _measure_inputs(app_engine, lab, mission_id)
    (job,) = inputs.captures
    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=c,
            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
            automatic_keypoint_prediction_id=job.automatic_keypoint_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            laser_calibration_id=inputs.calibration.laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
            length_m=0.42, depth_m=2.1, algorithm="laser_depth_fronto_parallel",
            algorithm_version="1", core_version="4.1.0",
        )
    ])  # fmt: skip
    return mission_id, c


async def test_a_length_is_current_and_drains_the_work(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)

    current = await _rows(
        owner_engine, "SELECT capture_id, length_m, producer, calibration_source "
        "FROM current_automatic_measurements",
    )  # fmt: skip
    assert current == [{"capture_id": c, "length_m": 0.42, "producer": "automatic",
                        "calibration_source": "label_free"}]  # fmt: skip
    assert (await _measure_inputs(app_engine, lab, mission_id)).captures == []


async def test_a_low_parallax_refusal_is_recorded_and_drains_the_work(
    owner_engine, app_engine
):
    """Measurement version 2 (contract v21, 0071): a dot too near the laser's
    vanishing point is refused, and the refusal answers the frame's work."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    inputs = await _in(app_engine, lab, automatic_measure_inputs, mission_id,
                       algorithm_version="2")  # fmt: skip
    (job,) = inputs.captures
    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=c,
            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
            automatic_keypoint_prediction_id=job.automatic_keypoint_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            laser_calibration_id=inputs.calibration.laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
            length_m=None, depth_m=52.2, refusal="low_parallax",
            algorithm="laser_depth_fronto_parallel", algorithm_version="2",
            core_version="4.3.0",
        )
    ])  # fmt: skip

    assert await _rows(
        owner_engine, "SELECT refusal FROM current_automatic_measurements"
    ) == [{"refusal": "low_parallax"}]
    assert (await _in(app_engine, lab, automatic_measure_inputs, mission_id,
                      algorithm_version="2")).captures == []  # fmt: skip


async def test_a_new_head_tail_makes_the_length_stale(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _predict(app_engine, lab, mission_id, _predicted(c, head_x=1700.0))

    assert (
        await _rows(owner_engine, "SELECT 1 FROM current_automatic_measurements") == []
    )
    assert [
        m.capture_id
        for m in (await _measure_inputs(app_engine, lab, mission_id)).captures
    ] == [c]


async def test_a_new_label_free_calibration_makes_a_length_stale(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())

    assert (
        await _rows(owner_engine, "SELECT 1 FROM current_automatic_measurements") == []
    )


async def test_a_length_on_a_stored_calibration_is_no_longer_current(
    owner_engine, app_engine
):
    """Lengths 0036 measured on production's calibration stay as history but
    leave the current results and the export (0064)."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    stored = await calibrate(owner_engine, lab, mission_id)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    (ht,) = await _rows(owner_engine, "SELECT id FROM automatic_head_tail_predictions")
    camera = await _camera(app_engine, lab, mission_id)
    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=c, automatic_head_tail_prediction_id=ht["id"],
            calibration_source="stored", laser_calibration_id=stored,
            camera_calibration_id=camera, length_m=0.42, depth_m=2.1,
            algorithm="laser_depth_fronto_parallel", algorithm_version="1",
            core_version="4.1.0",
        )
    ])  # fmt: skip

    assert await _rows(owner_engine, "SELECT 1 FROM automatic_measurements") != []
    assert (
        await _rows(owner_engine, "SELECT 1 FROM current_automatic_measurements") == []
    )
    assert await _rows(owner_engine, "SELECT 1 FROM automatic_results_export") == []


async def test_automatic_lengths_never_reach_the_human_path(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _measured(owner_engine, app_engine, lab)

    for view in ("measurements", "current_measurements", "measurement_work"):
        assert await _rows(owner_engine, f"SELECT 1 FROM {view}") == [], view


async def test_the_export_names_every_input_and_the_species(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    (job,) = await _in(app_engine, lab, automatic_species_captures, mission_id,
                       species_version=1)  # fmt: skip
    await _in(app_engine, lab, persist_automatic_species, mission_id, [
        AutomaticSpeciesRow(
            capture_id=c,
            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
            status="predicted", predictor_version=1,
            model_id="bioclip/2.5-vith14@x", predicted_choice=HOGFISH,
            top1_probability=0.7, margin=0.4,
            top5=[{"choice": HOGFISH, "probability": 0.7}],
        )
    ])  # fmt: skip

    (row,) = await _rows(owner_engine, "SELECT * FROM automatic_results_export")

    assert row["producer"] == "automatic"
    assert row["length_m"] == 0.42
    assert row["species_choice"] == HOGFISH
    assert row["species_producer"] == "automatic"
    assert row["sam_checkpoint"] == "sam3/3.1@abc"
    assert row["laser_predictor_version"] == 3
    assert row["calibration_source"] == "label_free"
    assert row["sam_score"] == 0.83
    assert row["capture_number"] is not None and row["dive_number"] is not None


async def test_the_export_drops_a_frame_the_detector_calls_a_slate(
    owner_engine, app_engine
):
    """A length written before the detector scored its frame, or before a
    re-score called it a slate, is a slate measured as a fish: it stays as
    history, never in the export (2026-10-15 gap audit)."""
    from _slate_calibration_seed import slate_presence as presence

    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    await presence(owner_engine, lab, c, probability=0.2)
    assert len(await _rows(owner_engine, "SELECT 1 FROM automatic_results_export")) == 1

    await presence(owner_engine, lab, c, probability=0.9)

    assert await _rows(owner_engine, "SELECT 1 FROM automatic_results_export") == []


async def test_the_export_flags_a_mission_humans_measured_since(
    owner_engine, app_engine
):
    """Decided 2026-10-15: a dive humans measure after its automatic run
    keeps its automatic lengths, flagged, so a reader can prefer the human
    ones or compare the two."""
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    (row,) = await _rows(owner_engine, "SELECT * FROM automatic_results_export")
    assert row["dive_human_measured"] is False

    # The humans' own calibration, which the automatic lengths do not use.
    cal = {"id": await calibrate(owner_engine, lab, mission_id)}
    other = await capture(owner_engine, lab, mission_id)
    await measurement(
        owner_engine, lab, other, await fish(owner_engine, lab), cal["id"]
    )

    (row,) = await _rows(owner_engine, "SELECT * FROM automatic_results_export")
    assert row["dive_human_measured"] is True


async def test_a_mission_whose_only_human_length_is_stale_is_not_flagged(
    owner_engine, app_engine
):
    """Review of #85: the flag read raw `measurements`, so a dive whose only
    human length rests on a superseded label (labels are superseded, never
    deleted) read as human-measured, and a reader preferring human lengths
    dropped its automatic ones for none. It reads `current_measurements`."""
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    # The humans' own calibration, which the automatic lengths do not use.
    cal = {"id": await calibrate(owner_engine, lab, mission_id)}
    other = await capture(owner_engine, lab, mission_id)
    gone = await head_tail_label(owner_engine, lab, other, superseded=True)
    await measurement(owner_engine, lab, other, await fish(owner_engine, lab),
                      cal["id"], head_tail_label_id=gone)  # fmt: skip

    (row,) = await _rows(owner_engine, "SELECT * FROM automatic_results_export")
    assert row["dive_human_measured"] is False


async def test_the_research_role_reads_the_export_for_the_lab(
    owner_engine, app_engine, research_engine
):
    lab = await tenant(owner_engine, "lab")
    await _measured(owner_engine, app_engine, lab)

    rows = await _rows(
        research_engine, "SELECT length_m FROM public.automatic_results_export"
    )
    assert rows == [{"length_m": 0.42}]


async def test_the_catalog_rechecks_membership(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    catalog = AutomaticResultsCatalog(app_engine, sub=ORCHESTRATOR)
    assert await catalog.member_tenants() == [tenants["lab"]]
    assert (
        await catalog.next_mission_for_automatic_results(tenants["lab"], **VERSIONS)
        is None
    )


# -- the slate detector, read by the cohort itself (0035) ---------------------------


async def test_a_detector_slate_frame_does_not_hold_its_mission_in_the_cohort(
    owner_engine, app_engine
):
    """A frame given a fish head/tail before the detector scored it is never
    measured once the detector calls it a slate, so it must not count as a
    length outstanding -- else its dive is never done, and the track (always
    the oldest dive) picks it every hour and stalls the backlog."""
    from _slate_calibration_seed import slate_presence as presence

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await calibrate(owner_engine, lab, mission_id)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    # Scored before the calibration, so the calibration is current.
    await presence(owner_engine, lab, c, probability=0.9)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip

    assert await _next(app_engine, lab) is None


async def test_a_detector_slate_frame_gets_no_species(owner_engine, app_engine):
    """A slate is not a fish to name: no BioCLIP job for it."""
    from _slate_calibration_seed import slate_presence as presence

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish = await capture(owner_engine, lab, mission_id)
    slate = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(fish), _predicted(slate))
    await presence(owner_engine, lab, slate, probability=0.9)

    jobs = await _in(app_engine, lab, automatic_species_captures, mission_id,
                     species_version=1)  # fmt: skip
    assert [j.capture_id for j in jobs] == [fish]


async def test_a_late_slate_score_makes_the_calibration_stale(owner_engine, app_engine):
    """Calibration candidates are the detector's slate frames; a score written
    after the dive's calibration may add candidates, so the dive is
    recalibrated -- not left on its 'no candidates' refusal."""
    from _slate_calibration_seed import slate_presence as presence

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=c, status="no_detections",
                                        laser_x=1.0, laser_y=2.0,
                                        predictor_version=1))  # fmt: skip
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip
    assert await _next(app_engine, lab) is None

    await presence(owner_engine, lab, c, probability=0.9)

    assert await _next(app_engine, lab) == mission_id


# -- dives the chain must leave alone, or only calibrate -----------------------------


async def test_an_excluded_mission_is_not_in_the_cohort(owner_engine, app_engine):
    """Some dives produce plausible but wrong results: 466 holds three laser
    states, 442 was shot overexposed (2026-10-15). An operator excludes them,
    with a reason."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await capture(owner_engine, lab, mission_id)
    assert await _next(app_engine, lab) == mission_id

    await exec_(
        owner_engine,
        "INSERT INTO automatic_results_exclusions (tenant_id, mission_id, reason) "
        "VALUES (:t, :d, 'three laser states')",
        t=lab,
        d=mission_id,
    )

    assert await _next(app_engine, lab) is None


async def test_an_exclusion_needs_a_reason(owner_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    with pytest.raises(IntegrityError):
        await exec_(
            owner_engine,
            "INSERT INTO automatic_results_exclusions (tenant_id, mission_id, reason) "
            "VALUES (:t, :d, '')",
            t=lab,
            d=mission_id,
        )


async def _calibration_session(owner_engine, lab):
    """A session (its slate frames) that a fish dive borrows from."""
    from _slate_calibration_seed import slate_presence as presence

    session = await _backlog_mission(owner_engine, lab)
    await _backlog_mission(owner_engine, lab, source_mission=session)
    slate = await capture(owner_engine, lab, session)
    other = await capture(owner_engine, lab, session)
    await presence(owner_engine, lab, slate, probability=0.95)
    await presence(owner_engine, lab, other, probability=0.05)
    return session, slate, other


async def test_a_calibration_session_runs_only_its_slate_frames(
    owner_engine, app_engine
):
    """Decided 2026-10-15: a session's slate frames feed the label-free fit
    its borrowers use; nothing in it is segmented or measured as a fish."""
    from fishsense_services_api.slate_presence_store import slate_frames

    lab = await tenant(owner_engine)
    session, slate, _ = await _calibration_session(owner_engine, lab)

    inputs = await _in(app_engine, lab, automatic_frames_inputs, session,
                       headtail_version=1, slate_frames=slate_frames)  # fmt: skip

    assert [f.capture_id for f in inputs.frames] == [slate]


async def test_a_calibrated_session_is_done_without_its_other_frames(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    session, slate, _ = await _calibration_session(owner_engine, lab)
    # Its borrower is a dive of the backlog too; done with it here.
    await exec_(
        owner_engine,
        "INSERT INTO automatic_results_exclusions (tenant_id, mission_id, reason) "
        "SELECT tenant_id, id, 'not this test' FROM missions "
        "WHERE calibration_source_mission_id = :s",
        s=session,
    )
    await _predict(app_engine, lab, session,
                   AutomaticHeadTailRow(capture_id=slate, status="slate_frame",
                                        laser_x=2000.0, laser_y=1500.0,
                                        predictor_version=1))  # fmt: skip
    assert await _next(app_engine, lab) == session  # its calibration is left

    await _in(app_engine, lab, persist_automatic_calibration, session,
              _label_free(capture_ids=[slate]))  # fmt: skip

    assert await _next(app_engine, lab) is None


async def test_a_session_frame_given_a_fish_is_never_measured_or_named(
    owner_engine, app_engine
):
    """A fish head/tail on a session (written before it was a session, or by
    the detector's miss) is no species job and no length."""
    lab = await tenant(owner_engine)
    session, _, other = await _calibration_session(owner_engine, lab)
    await calibrate(owner_engine, lab, session)
    await _predict(app_engine, lab, session, _predicted(other))

    jobs = await _in(app_engine, lab, automatic_species_captures, session,
                     species_version=1)  # fmt: skip
    measure = await _in(app_engine, lab, automatic_measure_inputs, session,
                        algorithm_version="1")  # fmt: skip

    assert jobs == []
    assert measure.captures == []


async def test_a_session_fish_is_no_length_outstanding(owner_engine, app_engine):
    """Its dive done otherwise, a fish head/tail on a session must not hold
    the session in the cohort for a length it will never get."""
    lab = await tenant(owner_engine)
    session, slate, other = await _calibration_session(owner_engine, lab)
    await exec_(
        owner_engine,
        "INSERT INTO automatic_results_exclusions (tenant_id, mission_id, reason) "
        "SELECT tenant_id, id, 'not this test' FROM missions "
        "WHERE calibration_source_mission_id = :s",
        s=session,
    )
    await calibrate(owner_engine, lab, session)
    await _predict(
        app_engine, lab, session,
        AutomaticHeadTailRow(capture_id=slate, status="slate_frame", laser_x=2000.0,
                             laser_y=1500.0, predictor_version=1),
        _predicted(other),
    )  # fmt: skip
    await _in(app_engine, lab, persist_automatic_calibration, session,
              _label_free(capture_ids=[slate]))  # fmt: skip

    assert await _next(app_engine, lab) is None


# -- the cohort as the backlog drains -----------------------------------------------

_BIG = {
    "automatic_head_tail_predictions",
    "automatic_species_predictions",
    "automatic_measurements",
    "slate_presence_predictions",
}


async def _copy_rows(owner_engine, table, template_id, where, params):
    """Copy one template row of `table` onto every capture `where` selects."""
    async with owner_engine.begin() as conn:
        columns = [
            name
            for (name,) in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name NOT IN "
                    "('id', 'seq', 'capture_id', 'created_at') "
                    "ORDER BY ordinal_position"
                ),
                {"t": table},
            )
        ]
        listed = ", ".join(columns)
        copied = ", ".join(f"r.{n}" for n in columns)
        await conn.execute(
            text(
                f"INSERT INTO {table} (capture_id, {listed}) "
                f"SELECT c.id, {copied} FROM {table} r JOIN captures c "
                f"ON c.tenant_id = r.tenant_id WHERE r.id = :template "
                f"AND c.id <> r.capture_id AND {where}"
            ),
            {"template": template_id, **params},
        )


async def _a_drained_backlog(owner_engine, app_engine, lab, *, missions=30, frames=30):
    """`missions` dives done (every frame abstained, slate-scored, a refused
    calibration), and one with a frame left: the cohort near the end."""
    from _slate_calibration_seed import slate_presence as presence

    device_id = await device(owner_engine, lab)
    done = []
    for _ in range(missions):
        d = await mission(owner_engine, lab, device_id=device_id, priority="low")
        done.append(d)
        for _ in range(frames):
            await capture(owner_engine, lab, d)
    first = done[0]
    template_capture = (
        await _rows(owner_engine, "SELECT id FROM captures WHERE mission_id = :d "
                    "LIMIT 1", d=first)
    )[0]["id"]  # fmt: skip
    await _predict(app_engine, lab, first,
                   AutomaticHeadTailRow(capture_id=template_capture,
                                        status="no_laser_dot", predictor_version=1))  # fmt: skip
    await presence(owner_engine, lab, template_capture, probability=0.05)
    (ht,) = await _rows(owner_engine, "SELECT id FROM automatic_head_tail_predictions")
    (sp,) = await _rows(owner_engine, "SELECT id FROM slate_presence_predictions")
    every = "c.mission_id = ANY(CAST(:missions AS uuid[]))"
    for table, row in (("automatic_head_tail_predictions", ht),
                       ("slate_presence_predictions", sp)):  # fmt: skip
        await _copy_rows(owner_engine, table, row["id"], every, {"missions": done})
    for d in done:
        await _in(app_engine, lab, persist_automatic_calibration, d,
                  _label_free(outcome="refused", refusal_reason="no_candidates",
                              laser_position=None, laser_axis=None))  # fmt: skip
    left = await mission(owner_engine, lab, device_id=device_id, priority="low")
    await capture(owner_engine, lab, left)
    await exec_(owner_engine, "ANALYZE")
    return left


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


async def test_the_last_mission_is_found_when_the_backlog_has_drained(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    left = await _a_drained_backlog(owner_engine, app_engine, lab)
    assert await _next(app_engine, lab) == left


async def test_the_cohort_reads_each_frames_latest_rows_by_index(
    owner_engine, app_engine
):
    """2026-10-15 audit: the cohort read every `current_automatic_*` view
    (and `current_slate_presence`) correlated per frame or per dive -- the
    shape that took the slate cohort past the app role's 30 s timeout once
    its backlog drained (2026-10-14). Each frame's latest row is one index
    lookup, and nothing de-duplicates the big tables wholesale."""
    lab = await tenant(owner_engine)
    await _a_drained_backlog(owner_engine, app_engine, lab)
    async with tenant_transaction(app_engine, lab) as conn:
        (plan,) = (
            await conn.execute(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT d.id FROM missions d "
                    f"WHERE d.tenant_id = :t AND {automatic_results_cohort()} "
                    "ORDER BY d.created_at, d.id LIMIT 1"
                ),
                {"t": lab, "hv": 1, "sv": 1, "cv": "1", "mv": "1"},
            )
        ).scalar_one()
    nodes = list(_plan_nodes(plan["Plan"]))
    # An empty table is read however Postgres likes -- a seq scan of nothing
    # is free; the drained dives here abstained, so no species or lengths.
    populated = {
        t for t in _BIG
        if (await _rows(owner_engine, f"SELECT 1 AS x FROM {t} LIMIT 1"))
    }  # fmt: skip
    assert {
        "automatic_head_tail_predictions",
        "slate_presence_predictions",
    } <= populated
    big_reads = [n for n in nodes if n.get("Relation Name") in populated]
    assert big_reads
    assert all(
        n["Node Type"] in ("Index Scan", "Index Only Scan") for n in big_reads
    ), [(n["Node Type"], n["Relation Name"]) for n in big_reads]
    assert not any(
        n["Node Type"] == "Unique"
        and {m.get("Relation Name") for m in _plan_nodes(n)} & _BIG
        for n in nodes
    )


# -- a dive that keeps failing does not hold the backlog ----------------------------


async def test_the_missions_a_run_already_took_are_not_offered_again(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    older = await _backlog_mission(owner_engine, lab)
    await capture(owner_engine, lab, older)
    newer = await _backlog_mission(owner_engine, lab)
    await capture(owner_engine, lab, newer)

    async def _next_but(*exclude):
        c = await _in(app_engine, lab, next_mission_for_automatic_results,
                      exclude=list(exclude), **VERSIONS)  # fmt: skip
        return None if c is None else c.mission_id

    assert await _next_but() == older
    assert await _next_but(older) == newer
    assert await _next_but(older, newer) is None


async def test_a_mission_that_failed_three_times_leaves_the_cohort(
    owner_engine, app_engine
):
    """2026-10-15 audit: the oldest dive, failing deterministically (a raw
    gone from the NAS, an unusable calibration, a bad frame), was selected
    every hour and starved the rest. Each failure is recorded; at three, the
    dive waits for an operator (who deletes its failures to retry it)."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await capture(owner_engine, lab, mission_id)

    for n in range(3):
        assert await _next(app_engine, lab) == mission_id, n
        await _in(app_engine, lab, record_automatic_results_failure, mission_id,
                  step="frames", error=f"attempt {n}")  # fmt: skip

    assert await _next(app_engine, lab) is None
    rows = await _rows(owner_engine, "SELECT step, error FROM "
                       "automatic_results_failures WHERE mission_id = :d", d=mission_id)  # fmt: skip
    assert len(rows) == 3 and rows[0]["step"] == "frames"


async def test_strikes_before_a_later_success_do_not_count(owner_engine, app_engine):
    """Review of #85: the strikes were cumulative and never reset, so three
    transient faults weeks apart -- each followed by a run that got further --
    left a healthy dive for an operator. A strike counts only if nothing of
    the dive's has been written since it: progress resets the count."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    await capture(owner_engine, lab, mission_id)

    for n in range(2):
        await _in(app_engine, lab, record_automatic_results_failure, mission_id,
                  step="frames", error=f"transient {n}")  # fmt: skip
    # A later run got further: one frame written.
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=a, status="no_laser_dot",
                                        predictor_version=1))  # fmt: skip
    await _in(app_engine, lab, record_automatic_results_failure, mission_id,
              step="frames", error="transient 2")  # fmt: skip

    assert await _next(app_engine, lab) == mission_id


async def test_strikes_with_no_progress_between_still_leave_the_mission(
    owner_engine, app_engine
):
    """The rule the reset must keep: a dive failing run after run, with
    nothing written between, waits for an operator."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a = await capture(owner_engine, lab, mission_id)
    await capture(owner_engine, lab, mission_id)
    # Progress first, then three failures with none between.
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=a, status="no_laser_dot",
                                        predictor_version=1))  # fmt: skip
    for n in range(3):
        await _in(app_engine, lab, record_automatic_results_failure, mission_id,
                  step="calibration", error=f"deterministic {n}")  # fmt: skip

    assert await _next(app_engine, lab) is None


async def test_a_failure_needs_a_known_step(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    with pytest.raises(DBAPIError):
        await _in(app_engine, lab, record_automatic_results_failure, mission_id,
                  step="sideways", error="x")  # fmt: skip


# -- a frame no worker can finish (2026-10-17 review of #85) ------------------------


async def _owed(app_engine, lab, mission_id):
    inputs = await _in(app_engine, lab, automatic_frames_inputs, mission_id,
                       headtail_version=VERSIONS["headtail_version"])  # fmt: skip
    return {f.capture_id for f in inputs.frames}


async def test_a_frame_left_out_is_still_owed_and_costs_the_mission_no_strike(
    owner_engine, app_engine
):
    """A frame the processor left out after its tries may be a transient
    fault (a pod torn down, a node preempted): it is recorded against the
    frame, never as one of the dive's three strikes, and the next run asks
    for it again."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)

    await _in(app_engine, lab, record_automatic_frame_failures, mission_id,
              [(c, "CUDA out of memory")],
              headtail_version=VERSIONS["headtail_version"])  # fmt: skip

    assert await _owed(app_engine, lab, mission_id) == {c}
    assert await _rows(owner_engine, "SELECT 1 FROM automatic_results_failures") == []
    assert await _next(app_engine, lab) == mission_id


async def test_a_frame_that_failed_in_three_runs_is_a_terminal_abstention(
    owner_engine, app_engine
):
    """Deterministic, then: a `frame_failed` row, so the frame drains and the
    dive goes on to its calibration and lengths instead of waiting for an
    operator with every other frame done."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    bad = await capture(owner_engine, lab, mission_id)
    good = await capture(owner_engine, lab, mission_id)

    for n in range(AUTOMATIC_FRAME_MAX_FAILURES):
        assert bad in await _owed(app_engine, lab, mission_id), n
        await _in(app_engine, lab, record_automatic_frame_failures, mission_id,
                  [(bad, f"run {n}")],
                  headtail_version=VERSIONS["headtail_version"])  # fmt: skip

    assert await _owed(app_engine, lab, mission_id) == {good}
    (row,) = await _rows(
        owner_engine,
        "SELECT status, predictor_version FROM automatic_head_tail_predictions "
        "WHERE capture_id = :c",
        c=bad,
    )
    assert row == {"status": "frame_failed",
                   "predictor_version": VERSIONS["headtail_version"]}  # fmt: skip


async def test_a_frame_failure_must_be_the_missions(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    other = await capture(owner_engine, lab, await _backlog_mission(owner_engine, lab))
    with pytest.raises(ForeignCapture):
        await _in(app_engine, lab, record_automatic_frame_failures, mission_id,
                  [(other, "x")], headtail_version=1)  # fmt: skip


async def test_an_unscored_mask_is_its_own_abstention(owner_engine, app_engine):
    """Not `laser_off_all_fish` (a scientific answer): a kernel/score mismatch
    is a bug to count, so it has its own status."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, AutomaticHeadTailRow(
        capture_id=c, status="mask_unscored",
        predictor_version=VERSIONS["headtail_version"],
        laser_x=1.0, laser_y=2.0,
    ))  # fmt: skip
    assert c not in await _owed(app_engine, lab, mission_id)


async def test_failures_at_an_older_version_do_not_count(owner_engine, app_engine):
    """A new head/tail version is a new chain (a fixed kernel, a new
    checkpoint): it gets its own tries."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    for n in range(AUTOMATIC_FRAME_MAX_FAILURES - 1):
        await _in(app_engine, lab, record_automatic_frame_failures, mission_id,
                  [(c, f"old {n}")], headtail_version=0)  # fmt: skip

    given_up = await _in(app_engine, lab, record_automatic_frame_failures, mission_id,
                         [(c, "new")], headtail_version=1)  # fmt: skip

    assert given_up == []
    assert await _owed(app_engine, lab, mission_id) == {c}


# -- stored masks (0059, 2026-10-21) ----------------------------------------------


def _mask(capture_id, key="masks/automatic/abc/1.png", **extra) -> PredictionMask:
    fields = dict(capture_id=capture_id, bucket="fishsense-tenant-lab", key=key,
                  sha256="a" * 64, size=900, content_type="image/png",
                  width=4000, height=3000)  # fmt: skip
    fields.update(extra)
    return PredictionMask(**fields)


async def test_a_prediction_and_its_mask_are_written_together(owner_engine, app_engine):
    """SAM's mask, stored in the tenant's bucket like the app's mask_u8, is
    named against the prediction that kept it, in one transaction."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish = await capture(owner_engine, lab, mission_id)
    empty = await capture(owner_engine, lab, mission_id)
    camera = await _camera(app_engine, lab, mission_id)
    rows = [replace(r, jpeg_render_version=1, jpeg_camera_calibration_id=camera)
            for r in (_predicted(fish), _predicted(empty, status="no_detections",
                      head_x=None, head_y=None, tail_x=None, tail_y=None,
                      mask_area_px=None, silhouette_ratio=None, mask_bbox=None,
                      sam_score=None))]  # fmt: skip

    await _in(app_engine, lab, persist_automatic_head_tails, mission_id, rows,
              masks=[_mask(fish), None])  # fmt: skip

    (row,) = await _rows(owner_engine, """
        SELECT m.object_key, m.width, m.height, m.capture_id, p.capture_id AS of
        FROM prediction_masks m
        JOIN automatic_head_tail_predictions p
          ON p.id = m.automatic_head_tail_prediction_id
        """)  # fmt: skip
    assert row == {"object_key": "masks/automatic/abc/1.png", "width": 4000,
                   "height": 3000, "capture_id": fish, "of": fish}  # fmt: skip


async def test_a_mask_for_no_prediction_in_the_batch_is_refused(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish = await capture(owner_engine, lab, mission_id)
    other = await capture(owner_engine, lab, mission_id)
    camera = await _camera(app_engine, lab, mission_id)
    row = replace(_predicted(fish), jpeg_render_version=1,
                  jpeg_camera_calibration_id=camera)  # fmt: skip

    with pytest.raises(ValueError):
        await _in(app_engine, lab, persist_automatic_head_tails, mission_id, [row],
                  masks=[_mask(other)])  # fmt: skip
    assert (
        await _rows(owner_engine, "SELECT id FROM automatic_head_tail_predictions")
        == []
    )


async def test_a_retried_persist_names_each_mask_once(owner_engine, app_engine):
    """Review of #232: a persist whose completion was lost is retried with the
    same masks; the key is named once, and the retry is not an error."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish = await capture(owner_engine, lab, mission_id)
    camera = await _camera(app_engine, lab, mission_id)
    row = replace(_predicted(fish), jpeg_render_version=1,
                  jpeg_camera_calibration_id=camera)  # fmt: skip
    for _ in range(2):
        await _in(app_engine, lab, persist_automatic_head_tails, mission_id, [row],
                  masks=[_mask(fish)])  # fmt: skip

    assert len(await _rows(owner_engine, "SELECT id FROM prediction_masks")) == 1


async def test_a_mask_names_exactly_one_prediction(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish = await capture(owner_engine, lab, mission_id)
    async with owner_engine.begin() as conn:
        with pytest.raises(IntegrityError):  # neither prediction
            await conn.execute(text("""
                INSERT INTO prediction_masks (tenant_id, capture_id, bucket,
                    object_key, sha256, size_bytes, content_type, width, height)
                VALUES (:t, :c, 'b', 'masks/x.png', :s, 1, 'image/png', 1, 1)
                """), {"t": lab, "c": fish, "s": "a" * 64})  # fmt: skip


# -- a mission's inputs read its frames' current rows, not the tenant's -------------


async def _a_measured_tenant(owner_engine, app_engine, lab, *, missions=20, frames=30):
    """One measured mission, and `missions` more whose every frame has a
    head/tail, a species and a length: the tenant as the run fills it."""
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    (job,) = await _in(app_engine, lab, automatic_species_captures, mission_id,
                       species_version=1)  # fmt: skip
    await _in(app_engine, lab, persist_automatic_species, mission_id, [
        AutomaticSpeciesRow(
            capture_id=c,
            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
            status="predicted", predictor_version=1, model_id="bioclip/2.5@x",
            predicted_choice=HOGFISH, top1_probability=0.7, margin=0.4,
            top5=[{"choice": HOGFISH, "probability": 0.7}],
        )
    ])  # fmt: skip
    device_id = await device(owner_engine, lab)
    others = []
    for _ in range(missions):
        d = await mission(owner_engine, lab, device_id=device_id, priority="low")
        others.append(d)
        for _ in range(frames):
            await capture(owner_engine, lab, d)
    every = "c.mission_id = ANY(CAST(:missions AS uuid[]))"
    for table in ("automatic_head_tail_predictions", "automatic_species_predictions",
                  "automatic_measurements"):  # fmt: skip
        (row,) = await _rows(owner_engine, f"SELECT id FROM {table} LIMIT 1")
        await _copy_rows(owner_engine, table, row["id"], every, {"missions": others})
    await exec_(owner_engine, "ANALYZE")
    return mission_id


#: The tables every input query reads a frame's latest row of.
_LATEST = {
    "automatic_head_tail_predictions",
    "automatic_species_predictions",
    "automatic_measurements",
}


@pytest.mark.parametrize("query", ["measure", "species", "calibration"])
async def test_a_missions_inputs_read_each_frames_current_rows_by_its_index(
    owner_engine, app_engine, query
):
    """2026-10-21: the measure-inputs query joined `current_automatic_*`
    views wholesale, and the planner, expecting one row, rebuilt the tenant's
    every current length once per frame -- 145 s for mission 312's 492
    frames, past the 30 s statement timeout, so lengths stopped being written
    as the run filled the tables. Each frame's current row is now looked up
    by its own (tenant, capture, seq) index."""
    lab = await tenant(owner_engine)
    mission_id = await _a_measured_tenant(owner_engine, app_engine, lab)
    sql = {
        "measure": AUTOMATIC_MEASURE_CAPTURES,
        "species": AUTOMATIC_SPECIES_CAPTURES,
        "calibration": AUTOMATIC_CALIBRATION_CAPTURES,
    }[query]
    async with tenant_transaction(app_engine, lab) as conn:
        (plan,) = (
            await conn.execute(
                text(f"EXPLAIN (FORMAT JSON) {sql}"),
                {"t": lab, "d": mission_id, "mv": "1", "sv": 1},
            )
        ).scalar_one()
    reads = [n for n in _plan_nodes(plan["Plan"]) if n.get("Relation Name") in _LATEST]
    assert reads
    assert all(
        n["Node Type"] in ("Index Scan", "Index Only Scan", "Index Scan Backward")
        and "capture_id" in n.get("Index Cond", "")
        for n in reads
    ), [(n["Node Type"], n["Relation Name"], n.get("Index Cond")) for n in reads]


async def test_the_missions_with_automatic_dots(owner_engine, app_engine):
    """Calibration v3's replay (2026-10-22): every mission a dot was found in."""
    from fishsense_services_api.automatic_results_store import (
        missions_with_automatic_dots,
    )

    lab = await tenant(owner_engine)
    dotted, dotless, untouched = [
        await _backlog_mission(owner_engine, lab) for _ in range(3)
    ]
    await _predict(
        app_engine, lab, dotted, _predicted(await capture(owner_engine, lab, dotted))
    )
    await _predict(app_engine, lab, dotless, _predicted(
        await capture(owner_engine, lab, dotless), status="no_laser_dot", laser_x=None,
        laser_y=None, head_x=None, head_y=None, tail_x=None, tail_y=None,
        mask_area_px=None, crop_x=None, crop_y=None, mask_bbox=None, sam_score=None))  # fmt: skip

    found = await _in(app_engine, lab, missions_with_automatic_dots)
    assert [m for m, _ in found] == [dotted]
