"""Automatic head/tail v5: SimCC keypoints beside the SAM rule (owner,
2026-10-24/25), the database side.

The SAM rule (the mask's extreme points) is broken on real fish; fishsense-
core's SimCC model on the same SAM box replaces it, except on the fish
models, which keep the SAM rule. A model frame is one in a ``known_length``
mission or whose human species label names a Fish Model (exactly the 29
model missions). Where the two disagree on length by more than 15% the frame
goes to review and is not measured (11% of non-model frames on the
2026-10-24 replay). Pinned here:

* keypoint work: each non-model frame's current SAM prediction, rendered,
  with no keypoints at the current version -- never a model frame, a slate
  frame, an unrendered frame, or one already done;
* the persist flags a disagreement over `KEYPOINT_DISAGREEMENT`, records a
  refused box, records both lengths, and skips a prediction no longer the
  frame's current one;
* the measure inputs: SimCC's points for a non-model frame (with the
  keypoints' id), the SAM rule's for a model frame, nothing until a
  non-model frame has keypoints, and a refusal for one to review;
* a length measured before the keypoints is owed again; one measured from
  them is current; the cohort owes keypoints and drains when they are done.
"""

import math

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    capture,
    forget_identities,
    species_label,
    tenant,
)
from fishsense_services_api.automatic_results_store import (
    KEYPOINT_DISAGREEMENT,
    KEYPOINTS_PREDICTOR_VERSION,
    AutomaticHeadTailRow,
    AutomaticKeypointRow,
    AutomaticMeasurementRow,
    automatic_keypoint_inputs,
    persist_automatic_calibration,
    persist_automatic_keypoints,
    persist_automatic_measurements,
)
from test_automatic_results_store import (
    _backlog_mission,
    _in,
    _label_free,
    _measure_inputs,
    _next,
    _predict,
    _predicted,
)

pytestmark = pytest.mark.usefixtures("forget_identities")

#: `_predicted`'s SAM-rule head and tail: 500.4 px apart.
SAM_HEAD, SAM_TAIL = (1800.0, 1500.0), (2300.0, 1480.0)
SAM_LENGTH = math.hypot(500.0, 20.0)


async def _known_length(owner_engine, mission_id):
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE missions SET purpose = 'known_length' WHERE id = :m"),
            {"m": mission_id},
        )


async def _frame(owner_engine, app_engine, lab, mission_id, **row):
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c, **row), keypoints=False)
    return c, await _current_prediction(owner_engine, c)


async def _current_prediction(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM current_automatic_head_tail_predictions "
                     "WHERE capture_id = :c"), {"c": capture_id})
        ).scalar_one()  # fmt: skip


async def _work(app_engine, lab, mission_id):
    return await _in(app_engine, lab, automatic_keypoint_inputs, mission_id)


def _simcc(capture_id, prediction_id, *, scale=1.0, status="predicted"):
    """SimCC's answer: the SAM rule's points, its length scaled by `scale`."""
    if status != "predicted":
        return AutomaticKeypointRow(prediction_id=prediction_id, capture_id=capture_id,
                                    status=status, checkpoint="simcc@e12")  # fmt: skip
    hx, hy = SAM_HEAD
    tx, ty = SAM_TAIL
    return AutomaticKeypointRow(
        prediction_id=prediction_id, capture_id=capture_id, status="predicted",
        head=[hx, hy], tail=[hx + (tx - hx) * scale, hy + (ty - hy) * scale],
        confidence=0.4, crop=[1300, 760, 2450, 1880], checkpoint="simcc@e12",
    )  # fmt: skip


async def _keypoints(owner_engine):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("""
                    SELECT capture_id, status, sam_rule_length_px, simcc_length_px,
                           disagreement, predictor_version
                    FROM automatic_keypoint_predictions ORDER BY seq
                    """))).all()


# -- the work -----------------------------------------------------------------------


async def test_keypoint_work_is_each_non_model_rendered_prediction(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    fish, h = await _frame(owner_engine, app_engine, lab, mission_id)
    labelled_model, _ = await _frame(owner_engine, app_engine, lab, mission_id)
    await species_label(owner_engine, lab, labelled_model, "Fish Model, Grouper")
    unrendered = await capture(owner_engine, lab, mission_id)
    await _predict(
        app_engine,
        lab,
        mission_id,
        _predicted(unrendered),
        rendered=False,
        keypoints=False,
    )
    slate = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, AutomaticHeadTailRow(
        capture_id=slate, status="slate_frame", laser_x=1.0, laser_y=2.0,
        predictor_version=1))  # fmt: skip

    work = await _work(app_engine, lab, mission_id)

    assert [(w.capture_id, w.prediction_id) for w in work] == [(fish, h)]
    assert work[0].mask_bbox and work[0].jpeg_render_version == 1


async def test_a_known_length_missions_frames_keep_the_sam_rule(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _frame(owner_engine, app_engine, lab, mission_id)
    await _known_length(owner_engine, mission_id)

    assert await _work(app_engine, lab, mission_id) == []


# -- the persist --------------------------------------------------------------------


@pytest.mark.parametrize(
    "scale, status",
    [(1.0, "predicted"), (1.10, "predicted"), (1.20, "keypoints_disagree"),
     (0.80, "keypoints_disagree")],
)  # fmt: skip
async def test_a_disagreement_over_the_threshold_goes_to_review(
    owner_engine, app_engine, scale, status
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, h = await _frame(owner_engine, app_engine, lab, mission_id)

    written = await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
                        [_simcc(c, h, scale=scale)])  # fmt: skip

    (row,) = await _keypoints(owner_engine)
    assert written == 1
    assert row.status == status
    assert row.sam_rule_length_px == pytest.approx(SAM_LENGTH)
    assert row.simcc_length_px == pytest.approx(SAM_LENGTH * scale)
    assert row.disagreement == pytest.approx(abs(scale - 1.0))
    assert row.predictor_version == KEYPOINTS_PREDICTOR_VERSION
    assert KEYPOINT_DISAGREEMENT == 0.15
    assert await _work(app_engine, lab, mission_id) == []


async def test_a_refused_box_is_recorded_for_review(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, h = await _frame(owner_engine, app_engine, lab, mission_id)

    await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
              [_simcc(c, h, status="refused")])  # fmt: skip

    (row,) = await _keypoints(owner_engine)
    assert (row.status, row.simcc_length_px) == ("keypoints_refused", None)


async def test_keypoints_for_a_superseded_prediction_are_not_written(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, old = await _frame(owner_engine, app_engine, lab, mission_id)
    await _predict(
        app_engine, lab, mission_id, _predicted(c, head_x=1700.0), keypoints=False
    )

    written = await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
                        [_simcc(c, old)])  # fmt: skip

    assert written == 0 and await _keypoints(owner_engine) == []


# -- the lengths --------------------------------------------------------------------


async def _calibrated(app_engine, lab, mission_id):
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())


async def test_a_non_model_frame_is_measured_from_simccs_points_once_it_has_them(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, h = await _frame(owner_engine, app_engine, lab, mission_id)
    await _calibrated(app_engine, lab, mission_id)
    before = await _measure_inputs(app_engine, lab, mission_id)
    assert (before.captures, before.refusals) == ([], [])  # waiting, not refused

    await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
              [_simcc(c, h, scale=1.05)])  # fmt: skip
    (job,) = (await _measure_inputs(app_engine, lab, mission_id)).captures

    assert job.automatic_keypoint_prediction_id is not None
    assert (job.head_x, job.head_y) == SAM_HEAD
    assert job.tail_x == pytest.approx(SAM_HEAD[0] + 500.0 * 1.05)


async def test_a_model_frame_is_measured_from_the_sam_rule(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _known_length(owner_engine, mission_id)
    await _frame(owner_engine, app_engine, lab, mission_id)
    await _calibrated(app_engine, lab, mission_id)

    (job,) = (await _measure_inputs(app_engine, lab, mission_id)).captures

    assert job.automatic_keypoint_prediction_id is None
    assert (job.tail_x, job.tail_y) == SAM_TAIL


async def test_a_frame_to_review_is_a_refusal_not_a_measurement(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, h = await _frame(owner_engine, app_engine, lab, mission_id)
    await _calibrated(app_engine, lab, mission_id)
    await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
              [_simcc(c, h, scale=1.3)])  # fmt: skip

    inputs = await _measure_inputs(app_engine, lab, mission_id)

    assert inputs.captures == []
    assert [(r.capture_id, r.reason) for r in inputs.refusals] == [
        (c, "keypoints_disagree")
    ]


def _length(inputs, job, **extra):
    fields = dict(
        capture_id=job.capture_id,
        automatic_head_tail_prediction_id=job.automatic_head_tail_prediction_id,
        automatic_keypoint_prediction_id=job.automatic_keypoint_prediction_id,
        calibration_source=inputs.calibration.source,
        automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
        camera_calibration_id=inputs.camera_calibration_id,
        length_m=0.4, depth_m=2.0, algorithm="laser_depth_fronto_parallel",
        algorithm_version="1", core_version="4.3.2",
    )  # fmt: skip
    return AutomaticMeasurementRow(**{**fields, **extra})


async def _owed(app_engine, lab):
    """The cohort's next dive with species off: a rendered fish owes species
    as well, and that is not this module's work."""
    from fishsense_services_api.automatic_results_store import (
        next_mission_for_automatic_results,
    )
    from test_automatic_results_store import VERSIONS

    found = await _in(app_engine, lab, next_mission_for_automatic_results,
                      **VERSIONS, species=False)  # fmt: skip
    return None if found is None else found.mission_id


async def test_lengths_follow_the_frames_keypoints_and_the_cohort_drains(
    owner_engine, app_engine
):
    """A SAM-rule length from before v5 is owed again once the frame has
    keypoints; one measured from them is current; then nothing is owed."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, h = await _frame(owner_engine, app_engine, lab, mission_id, laser_x=2000.0)
    await _calibrated(app_engine, lab, mission_id)
    assert await _owed(app_engine, lab) == mission_id  # keypoints owed

    await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
              [_simcc(c, h)])  # fmt: skip
    inputs = await _measure_inputs(app_engine, lab, mission_id)
    (job,) = inputs.captures
    stale = _length(inputs, job, automatic_keypoint_prediction_id=None)
    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [stale])
    assert len((await _measure_inputs(app_engine, lab, mission_id)).captures) == 1

    await _in(app_engine, lab, persist_automatic_measurements, mission_id,
              [_length(inputs, job)])  # fmt: skip
    assert (await _measure_inputs(app_engine, lab, mission_id)).captures == []
    assert await _owed(app_engine, lab) is None


async def test_a_refusal_for_review_drains_the_work(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c, h = await _frame(owner_engine, app_engine, lab, mission_id)
    await _calibrated(app_engine, lab, mission_id)
    await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
              [_simcc(c, h, scale=1.3)])  # fmt: skip
    inputs = await _measure_inputs(app_engine, lab, mission_id)
    (ref,) = inputs.refusals

    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=ref.capture_id,
            automatic_head_tail_prediction_id=ref.automatic_head_tail_prediction_id,
            automatic_keypoint_prediction_id=ref.automatic_keypoint_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            refusal=ref.reason, algorithm="laser_depth_fronto_parallel",
            algorithm_version="1", core_version="4.3.2")])  # fmt: skip

    assert (await _measure_inputs(app_engine, lab, mission_id)).refusals == []
    assert await _owed(app_engine, lab) is None


async def test_a_length_naming_another_frames_keypoints_is_refused(
    owner_engine, app_engine
):
    from fishsense_services_api.automatic_results_store import ForeignHeadTail

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    a, ha = await _frame(owner_engine, app_engine, lab, mission_id)
    b, hb = await _frame(owner_engine, app_engine, lab, mission_id)
    await _calibrated(app_engine, lab, mission_id)
    await _in(app_engine, lab, persist_automatic_keypoints, mission_id,
              [_simcc(a, ha), _simcc(b, hb)])  # fmt: skip
    inputs = await _measure_inputs(app_engine, lab, mission_id)
    ja, jb = sorted(inputs.captures, key=lambda j: j.capture_id != a)

    with pytest.raises(ForeignHeadTail, match="keypoints"):
        await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
            _length(inputs, ja,
                    automatic_keypoint_prediction_id=jb.automatic_keypoint_prediction_id)
        ])  # fmt: skip
