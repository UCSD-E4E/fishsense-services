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

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    PLAUSIBLE_POSITION,
    calibrate,
    calibrated_dive,
    capture,
    device,
    dive,
    exec_,
    fish,
    forget_identities,
    measurable_capture,
    measurement,
    tenant,
)
from fishsense_services_api.automatic_results_store import (
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
    automatic_species_captures,
    next_dive_for_automatic_results,
    no_slate_frames,
    persist_automatic_calibration,
    persist_automatic_head_tails,
    persist_automatic_measurements,
    persist_automatic_species,
)
from fishsense_services_api.db import tenant_transaction

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


async def _backlog_dive(owner_engine, lab, *, priority="low", **kwargs):
    device_id = await device(owner_engine, lab)
    return await dive(
        owner_engine, lab, device_id=device_id, priority=priority, **kwargs
    )


async def _next(app_engine, lab):
    candidate = await _in(app_engine, lab, next_dive_for_automatic_results, **VERSIONS)
    return None if candidate is None else candidate.dive_id


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


async def _predict(app_engine, lab, dive_id, *rows):
    return await _in(app_engine, lab, persist_automatic_head_tails, dive_id, list(rows))


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
    dive_id = await _backlog_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(capture_id))

    async with tenant_transaction(app_engine, partner) as conn:
        seen = (
            await conn.execute(
                text("SELECT count(*) FROM automatic_head_tail_predictions")
            )
        ).scalar_one()
    assert seen == 0


async def test_every_row_is_produced_automatic(owner_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
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
    dive_id = await _backlog_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    with pytest.raises(DBAPIError, match="sam"):
        await _predict(app_engine, lab, dive_id, _predicted(capture_id, sam_score=0.49))


# -- the backlog cohort -------------------------------------------------------------


@pytest.mark.parametrize("priority", ["low", "none", "high"])
async def test_the_cohort_is_any_priority(owner_engine, app_engine, priority):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab, priority=priority)
    await capture(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id


async def test_a_dive_with_no_capture_has_no_work(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _backlog_dive(owner_engine, lab)
    assert await _next(app_engine, lab) is None


async def test_a_dive_with_a_human_measurement_is_never_selected(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    measured, cal = await calibrated_dive(owner_engine, lab, priority="low")
    c = await measurable_capture(owner_engine, lab, measured)
    await measurement(owner_engine, lab, c, await fish(owner_engine, lab), cal)

    assert await _next(app_engine, lab) is None


async def test_oldest_first(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    newer = await _backlog_dive(owner_engine, lab)
    older = await _backlog_dive(owner_engine, lab)
    await exec_(owner_engine, "UPDATE dives SET created_at = created_at - "
                "interval '1 day' WHERE id = :d", d=older)  # fmt: skip
    for d in (newer, older):
        await capture(owner_engine, lab, d)

    assert await _next(app_engine, lab) == older


async def test_a_dive_the_pipeline_cannot_rectify_is_not_selected(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    uncalibrated = await device(owner_engine, lab, camera_matrix=False)
    d = await dive(owner_engine, lab, device_id=uncalibrated, priority="low")
    await capture(owner_engine, lab, d)
    none = await dive(owner_engine, lab, priority="low")
    await capture(owner_engine, lab, none)

    assert await _next(app_engine, lab) is None


async def test_the_cohort_drains(owner_engine, app_engine):
    """Every frame predicted (or abstained), a calibration row (even a
    refusal), no length possible without a calibration: done."""
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    fish_frame = await capture(owner_engine, lab, dive_id)
    empty_frame = await capture(owner_engine, lab, dive_id)
    await capture(owner_engine, lab, dive_id, canonical=False)
    assert await _next(app_engine, lab) == dive_id

    await _predict(
        app_engine, lab, dive_id, _predicted(fish_frame),
        AutomaticHeadTailRow(capture_id=empty_frame, status="no_laser_dot",
                             predictor_version=1),
    )  # fmt: skip
    assert await _next(app_engine, lab) == dive_id  # species, calibration left

    (job,) = await _in(app_engine, lab, automatic_species_captures, dive_id,
                       species_version=1)  # fmt: skip
    await _in(app_engine, lab, persist_automatic_species, dive_id, [
        AutomaticSpeciesRow(capture_id=fish_frame,
                            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
                            status="decode_failed", predictor_version=1, model_id="m")
    ])  # fmt: skip
    assert await _next(app_engine, lab) == dive_id  # calibration left

    await _in(app_engine, lab, persist_automatic_calibration, dive_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip
    # Refused and no stored calibration: no length is possible, so no work.
    assert await _next(app_engine, lab) is None


async def test_a_new_version_reopens_the_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c, predictor_version=0))

    inputs = await _in(app_engine, lab, automatic_frames_inputs, dive_id,
                       headtail_version=1)  # fmt: skip
    assert [f.capture_id for f in inputs.frames] == [c]


async def test_new_frames_make_the_calibration_stale(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    a = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id,
                   AutomaticHeadTailRow(capture_id=a, status="no_laser_dot",
                                        predictor_version=1))  # fmt: skip
    await _in(app_engine, lab, persist_automatic_calibration, dive_id,
              _label_free(outcome="refused", refusal_reason="no_candidates",
                          laser_position=None, laser_axis=None))  # fmt: skip
    assert await _next(app_engine, lab) is None

    b = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id,
                   AutomaticHeadTailRow(capture_id=b, status="slate_frame",
                                        laser_x=1.0, laser_y=2.0,
                                        predictor_version=1))  # fmt: skip
    assert await _next(app_engine, lab) == dive_id


# -- frames: what the GPU stage runs on ----------------------------------------------


async def test_frames_are_the_canonical_captures_with_their_intrinsics(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    a = await capture(owner_engine, lab, dive_id)
    await capture(owner_engine, lab, dive_id, canonical=False)

    inputs = await _in(app_engine, lab, automatic_frames_inputs, dive_id,
                       headtail_version=1)  # fmt: skip

    assert [f.capture_id for f in inputs.frames] == [a]
    assert inputs.frames[0].slate_probability is None
    assert inputs.camera_matrix[0][0] == 3000.0
    assert len(inputs.distortion_coefficients) == 5


async def test_slate_frames_ride_on_the_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    slate = await capture(owner_engine, lab, dive_id)
    fish_frame = await capture(owner_engine, lab, dive_id)

    async def detector(conn, tenant_id, d):
        assert (tenant_id, d) == (lab, dive_id)
        return [(slate, 0.97), (fish_frame, 0.2)]

    inputs = await _in(app_engine, lab, automatic_frames_inputs, dive_id,
                       headtail_version=1, slate_frames=detector)  # fmt: skip

    by = {f.capture_id: f for f in inputs.frames}
    assert by[slate].is_slate and by[slate].slate_probability == 0.97
    assert not by[fish_frame].is_slate
    assert SLATE_FRAME_THRESHOLD == 0.5


async def test_the_slate_interface_is_stubbed_to_none(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    assert await _in(app_engine, lab, no_slate_frames, dive_id) == []


# -- persisting the GPU stage's output ---------------------------------------------


async def test_rows_for_another_dive_are_refused_and_nothing_is_written(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    other = await _backlog_dive(owner_engine, lab)
    mine = await capture(owner_engine, lab, dive_id)
    theirs = await capture(owner_engine, lab, other)

    with pytest.raises(ForeignCapture):
        await _predict(app_engine, lab, dive_id, _predicted(mine), _predicted(theirs))
    assert (
        await _rows(owner_engine, "SELECT 1 FROM automatic_head_tail_predictions") == []
    )


async def test_the_human_path_is_untouched(owner_engine, app_engine):
    """No label, no human-path prediction, no measurement."""
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c))

    for table in ("laser_labels", "head_tail_labels", "species_labels",
                  "laser_predictions", "head_tail_predictions",
                  "species_predictions", "measurements", "laser_calibrations"):  # fmt: skip
        assert await _rows(owner_engine, f"SELECT 1 FROM {table}") == [], table


# -- species: BioCLIP zero-shot on the automatic mask ------------------------------


async def test_species_jobs_are_the_predicted_masks(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    a, b = await capture(owner_engine, lab, dive_id), await capture(
        owner_engine, lab, dive_id
    )
    await _predict(app_engine, lab, dive_id, _predicted(a),
                   AutomaticHeadTailRow(capture_id=b, status="no_detections",
                                        predictor_version=1))  # fmt: skip

    (job,) = await _in(app_engine, lab, automatic_species_captures, dive_id,
                       species_version=1)  # fmt: skip

    assert job.capture_id == a and job.mask_bbox == BOX


async def test_species_naming_another_captures_mask_is_refused(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    a, b = await capture(owner_engine, lab, dive_id), await capture(
        owner_engine, lab, dive_id
    )
    await _predict(app_engine, lab, dive_id, _predicted(a), _predicted(b))
    jobs = await _in(app_engine, lab, automatic_species_captures, dive_id,
                     species_version=1)  # fmt: skip
    by = {j.capture_id: j.automatic_head_tail_prediction_id for j in jobs}

    with pytest.raises(ForeignHeadTail):
        await _in(app_engine, lab, persist_automatic_species, dive_id, [
            AutomaticSpeciesRow(capture_id=a, automatic_head_tail_prediction_id=by[b],
                                status="decode_failed", predictor_version=1,
                                model_id="m")])  # fmt: skip


# -- calibration ---------------------------------------------------------------------


async def test_calibration_candidates_are_slate_frames_with_a_dot(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    on = await capture(owner_engine, lab, dive_id)
    nodot = await capture(owner_engine, lab, dive_id)
    fish_frame = await capture(owner_engine, lab, dive_id)
    await _predict(
        app_engine, lab, dive_id,
        AutomaticHeadTailRow(capture_id=on, status="slate_frame", laser_x=10.0,
                             laser_y=20.0, slate_probability=0.9, predictor_version=1),
        AutomaticHeadTailRow(capture_id=nodot, status="no_laser_dot",
                             slate_probability=0.9, predictor_version=1),
        _predicted(fish_frame),
    )  # fmt: skip

    async def detector(conn, tenant_id, d):
        return [(on, 0.9), (nodot, 0.9), (fish_frame, 0.1)]

    inputs = await _in(app_engine, lab, automatic_calibration_inputs, dive_id,
                       slate_frames=detector)  # fmt: skip

    assert [(c.capture_id, c.x, c.y) for c in inputs.candidates] == [(on, 10.0, 20.0)]
    # The dive line is fitted from every automatic dot of the dive.
    assert sorted(map(tuple, inputs.line_dots)) == [(10.0, 20.0), (2000.0, 1500.0)]
    assert inputs.camera_matrix[0][0] == 3000.0


async def _measure_inputs(app_engine, lab, dive_id):
    return await _in(app_engine, lab, automatic_measure_inputs, dive_id,
                     algorithm_version="1")  # fmt: skip


async def test_lengths_use_the_dives_own_label_free_calibration_first(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    await calibrate(owner_engine, lab, dive_id)  # a stored one, too
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c))
    cal = await _in(
        app_engine, lab, persist_automatic_calibration, dive_id, _label_free()
    )

    inputs = await _measure_inputs(app_engine, lab, dive_id)

    assert inputs.calibration.source == "label_free"
    assert inputs.calibration.automatic_laser_calibration_id == cal
    assert inputs.calibration.laser_calibration_id is None
    assert [m.capture_id for m in inputs.captures] == [c]


async def test_then_the_calibration_links_label_free_one(owner_engine, app_engine):
    """Production's pairing: a fish dive borrows its calibration session's."""
    lab = await tenant(owner_engine)
    session = await _backlog_dive(owner_engine, lab)
    cal = await _in(
        app_engine, lab, persist_automatic_calibration, session, _label_free()
    )
    dive_id = await _backlog_dive(owner_engine, lab, source_dive=session)
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c))

    inputs = await _measure_inputs(app_engine, lab, dive_id)

    assert inputs.calibration.source == "label_free"
    assert inputs.calibration.automatic_laser_calibration_id == cal


async def test_a_dives_own_label_free_fit_beats_its_links(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    session = await _backlog_dive(owner_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, session, _label_free())
    dive_id = await _backlog_dive(owner_engine, lab, source_dive=session)
    own = await _in(
        app_engine, lab, persist_automatic_calibration, dive_id, _label_free()
    )
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c))

    inputs = await _measure_inputs(app_engine, lab, dive_id)

    assert inputs.calibration.automatic_laser_calibration_id == own


async def test_then_the_stored_calibration(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    stored = await calibrate(owner_engine, lab, dive_id, axis=(0.0, 0.0, 1.0))
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c))
    await _in(app_engine, lab, persist_automatic_calibration, dive_id,
              _label_free(outcome="refused", refusal_reason="too_few_frames",
                          laser_position=None, laser_axis=None))  # fmt: skip

    inputs = await _measure_inputs(app_engine, lab, dive_id)

    assert inputs.calibration.source == "stored"
    assert inputs.calibration.laser_calibration_id == stored
    assert inputs.calibration.laser_position == PLAUSIBLE_POSITION


async def test_slate_frames_are_never_measured(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await _backlog_dive(owner_engine, lab)
    await calibrate(owner_engine, lab, dive_id)
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id,
                   AutomaticHeadTailRow(capture_id=c, status="slate_frame",
                                        laser_x=1.0, laser_y=2.0,
                                        predictor_version=1))  # fmt: skip
    assert (await _measure_inputs(app_engine, lab, dive_id)).captures == []


# -- measurements: current, stale, never the human path's -------------------------


async def _measured(owner_engine, app_engine, lab):
    dive_id = await _backlog_dive(owner_engine, lab)
    await calibrate(owner_engine, lab, dive_id)
    c = await capture(owner_engine, lab, dive_id)
    await _predict(app_engine, lab, dive_id, _predicted(c))
    inputs = await _measure_inputs(app_engine, lab, dive_id)
    (job,) = inputs.captures
    await _in(app_engine, lab, persist_automatic_measurements, dive_id, [
        AutomaticMeasurementRow(
            capture_id=c,
            automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            laser_calibration_id=inputs.calibration.laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
            length_m=0.42, depth_m=2.1, algorithm="laser_depth_fronto_parallel",
            algorithm_version="1", core_version="4.1.0",
        )
    ])  # fmt: skip
    return dive_id, c


async def test_a_length_is_current_and_drains_the_work(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, c = await _measured(owner_engine, app_engine, lab)

    current = await _rows(
        owner_engine, "SELECT capture_id, length_m, producer, calibration_source "
        "FROM current_automatic_measurements",
    )  # fmt: skip
    assert current == [{"capture_id": c, "length_m": 0.42, "producer": "automatic",
                        "calibration_source": "stored"}]  # fmt: skip
    assert (await _measure_inputs(app_engine, lab, dive_id)).captures == []


async def test_a_new_head_tail_makes_the_length_stale(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, c = await _measured(owner_engine, app_engine, lab)
    await _predict(app_engine, lab, dive_id, _predicted(c, head_x=1700.0))

    assert (
        await _rows(owner_engine, "SELECT 1 FROM current_automatic_measurements") == []
    )
    assert [
        m.capture_id for m in (await _measure_inputs(app_engine, lab, dive_id)).captures
    ] == [c]


async def test_a_label_free_calibration_makes_a_stored_length_stale(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, c = await _measured(owner_engine, app_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, dive_id, _label_free())

    assert (
        await _rows(owner_engine, "SELECT 1 FROM current_automatic_measurements") == []
    )


async def test_automatic_lengths_never_reach_the_human_path(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _measured(owner_engine, app_engine, lab)

    for view in ("measurements", "current_measurements", "measurement_work"):
        assert await _rows(owner_engine, f"SELECT 1 FROM {view}") == [], view


async def test_the_export_names_every_input_and_the_species(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, c = await _measured(owner_engine, app_engine, lab)
    (job,) = await _in(app_engine, lab, automatic_species_captures, dive_id,
                       species_version=1)  # fmt: skip
    await _in(app_engine, lab, persist_automatic_species, dive_id, [
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
    assert row["calibration_source"] == "stored"
    assert row["sam_score"] == 0.83
    assert row["capture_number"] is not None and row["dive_number"] is not None


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
        await catalog.next_dive_for_automatic_results(tenants["lab"], **VERSIONS)
        is None
    )
