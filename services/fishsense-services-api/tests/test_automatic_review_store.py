"""Reviewing the automatic track (the owner, 2026-10-21: "use this app to
review the auto process").

Read-only. A frame shows what the automatic run made of it now: its status
(predicted, or why it abstained), the dot, head and tail, the mask, the
length, and the species guess. Its pixels are drawn on the render they were
predicted on, named by version and camera, which is not always the newest
render. A mission shows its run: frames by status, its label-free
calibration, its lengths and refusals, its failures, and how its lengths
compare with the human ones on the same frames.
"""

import pytest
from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrate,
    capture,
    exec_,
    fish,
    forget_identities,
    measurement,
    tenant,
)
from test_automatic_results_store import (
    HOGFISH,
    VERSIONS,  # noqa: F401
    _backlog_mission,
    _in,
    _label_free,
    _measured,
    _predict,
    _predicted,
)

from fishsense_services_api.automatic_results_store import (
    AutomaticSpeciesRow,
    persist_automatic_calibration,
    persist_automatic_species,
)
from fishsense_services_api.automatic_review_store import (
    automatic_frame,
    mission_automatic,
)
from fishsense_services_api.prediction_masks_store import PredictionMask


async def _render(owner_engine, lab, capture_id, *, new_camera=False) -> str:
    """A render of the frame under its mission's camera, the one the frames
    stage names; with ``new_camera``, under a newer camera calibration of the
    same device first (a recalibration)."""
    if new_camera:
        await exec_(
            owner_engine,
            "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
            "camera_matrix, distortion_coefficients) SELECT :t, m.device_id, "
            "'pinhole', '[[3100,0,2048],[0,3100,1536],[0,0,1]]', '[0,0,0,0,0]' "
            "FROM captures c JOIN missions m ON m.id = c.mission_id WHERE c.id = :c",
            t=lab,
            c=capture_id,
        )
    key = f"renders/r1/{capture_id.hex}-{'new' if new_camera else 'own'}.JPG"
    await exec_(
        owner_engine,
        "INSERT INTO capture_renders (tenant_id, capture_id, status, render_version, "
        "camera_calibration_id, bucket, key, bytes, md5, width, height) "
        "SELECT :t, c.id, 'rendered', 1, cc.id, 'fishsense-tenant-lab', :k, 5700000, "
        "'0123456789abcdef0123456789abcdef', 4014, 3016 FROM captures c "
        "JOIN missions m ON m.id = c.mission_id JOIN current_camera_calibrations cc "
        "ON cc.tenant_id = c.tenant_id AND cc.device_id = m.device_id "
        "AND cc.camera_model = 'pinhole' WHERE c.id = :c",
        t=lab,
        c=capture_id,
        k=key,
    )
    return key


async def _frame(app_engine, lab, capture_id):
    return await _in(app_engine, lab, automatic_frame, capture_id)


async def test_a_predicted_frame_shows_what_the_run_made_of_it(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    key = await _render(owner_engine, lab, c)

    found = await _frame(app_engine, lab, c)

    assert found.status == "predicted"
    assert (found.laser, found.head, found.tail) == (
        (2000.0, 1500.0),
        (1800.0, 1500.0),
        (2300.0, 1480.0),
    )
    assert found.sam_score == pytest.approx(0.83)
    assert (found.render_bucket, found.render_key) == ("fishsense-tenant-lab", key)
    assert (found.render_width, found.render_height) == (4014, 3016)
    assert (found.length_m, found.depth_m) == (0.42, 2.1)
    assert found.calibration_source == "label_free"
    assert found.refusal is None


async def test_its_pixels_go_on_the_render_they_were_predicted_on(
    owner_engine, app_engine
):
    """A newer render under a recalibrated camera is another image: the
    prediction's own render is the one to draw on."""
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    own = await _render(owner_engine, lab, c)
    await _render(owner_engine, lab, c, new_camera=True)

    assert (await _frame(app_engine, lab, c)).render_key == own


async def test_an_abstention_says_why_and_has_no_points(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(
        app_engine,
        lab,
        mission_id,
        _predicted(c, status="no_laser_dot", laser_x=None, laser_y=None,
                   laser_confidence=None, head_x=None, head_y=None, tail_x=None,
                   tail_y=None, mask_area_px=None, silhouette_ratio=None,
                   crop_x=None, crop_y=None, mask_bbox=None, sam_score=None),
    )  # fmt: skip

    found = await _frame(app_engine, lab, c)

    assert found.status == "no_laser_dot"
    assert (found.laser, found.head, found.tail, found.length_m) == (None,) * 4


async def test_a_frame_the_run_never_reached_has_nothing(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    c = await capture(owner_engine, lab, mission_id)

    assert await _frame(app_engine, lab, c) is None


async def test_a_length_made_stale_by_a_new_prediction_is_not_shown(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _predict(app_engine, lab, mission_id, _predicted(c, head_x=1700.0))

    found = await _frame(app_engine, lab, c)

    assert found.head == (1700.0, 1500.0)
    assert found.length_m is None


async def test_its_mask_and_species_come_with_it(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    mask = PredictionMask(
        capture_id=c, bucket="fishsense-tenant-lab", key=f"masks/automatic/{c}/m.png",
        sha256="ab" * 32, size=1234, content_type="image/png", width=4000, height=3000,
    )  # fmt: skip
    await _in(
        app_engine,
        lab,
        __import__(
            "fishsense_services_api.automatic_results_store", fromlist=["x"]
        ).persist_automatic_head_tails,
        mission_id,
        [_predicted(c)],
        [mask],
    )
    prediction = (await _frame(app_engine, lab, c)).prediction_id
    await _in(app_engine, lab, persist_automatic_species, mission_id, [
        AutomaticSpeciesRow(
            capture_id=c, automatic_head_tail_prediction_id=prediction,
            status="predicted", predictor_version=1, model_id="bioclip-2.5",
            predicted_choice="Hogfish", top1_probability=0.71, margin=0.4,
            top5=[{"choice": "Hogfish", "probability": 0.71}],
        )
    ])  # fmt: skip

    found = await _frame(app_engine, lab, c)

    assert (found.mask_bucket, found.mask_key) == (
        "fishsense-tenant-lab",
        f"masks/automatic/{c}/m.png",
    )
    assert (found.mask_width, found.mask_height) == (4000, 3000)
    assert (found.species, found.species_probability) == ("Hogfish", 0.71)


async def test_a_length_out_of_distribution_for_its_species_is_flagged(
    owner_engine, app_engine
):
    """2026-10-30: against the species BioCLIP names (`species_length`)."""
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    prediction = (await _frame(app_engine, lab, c)).prediction_id
    await _in(app_engine, lab, persist_automatic_species, mission_id, [
        AutomaticSpeciesRow(
            capture_id=c, automatic_head_tail_prediction_id=prediction,
            status="predicted", predictor_version=1, model_id="bioclip-2.5",
            predicted_choice=HOGFISH, top1_probability=0.71, margin=0.4,
            top5=[{"choice": HOGFISH, "probability": 0.71}],
        )
    ])  # fmt: skip

    found = await _frame(app_engine, lab, c)
    assert (found.species_length_flag, found.species_max_length_m) == (None, 0.91)

    await exec_(owner_engine, "UPDATE automatic_measurements SET length_m = 0.02 "
                "WHERE capture_id = :c", c=c)  # fmt: skip
    assert (await _frame(app_engine, lab, c)).species_length_flag == "short"


# -- a mission's run --------------------------------------------------------------


async def test_a_missions_run_counts_its_frames_by_status(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, measured = await _measured(owner_engine, app_engine, lab)
    abstained = await capture(owner_engine, lab, mission_id)
    await capture(owner_engine, lab, mission_id)  # never reached
    await _predict(
        app_engine, lab, mission_id,
        _predicted(abstained, status="no_detections", head_x=None, head_y=None,
                   tail_x=None, tail_y=None, mask_area_px=None, silhouette_ratio=None,
                   crop_x=None, crop_y=None, mask_bbox=None, sam_score=None),
    )  # fmt: skip

    run = await _in(app_engine, lab, mission_automatic, mission_id)

    assert run.frames == 3
    assert run.by_status == {"predicted": 1, "no_detections": 1}
    assert run.unprocessed == 1
    assert (run.lengths, run.refusals) == (1, {})


async def test_a_missions_label_free_calibration_and_its_outcome(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              _label_free(outcome="refused", refusal_reason="too few pairs",
                          laser_position=None, laser_axis=None, vanishing_px=None,
                          line_direction=None, line_offset_px=None, o_mag_m=None))  # fmt: skip

    run = await _in(app_engine, lab, mission_automatic, mission_id)

    assert run.calibration.outcome == "refused"
    assert run.calibration.refusal_reason == "too few pairs"
    # The automatic stack alone (0064): with its fit refused, nothing to measure on.
    assert run.lengths_use is None


async def test_its_lengths_are_compared_with_the_human_ones_on_the_same_frames(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)  # 0.42 m
    calibration = await _in(app_engine, lab, mission_automatic, mission_id)
    the_fish = await fish(owner_engine, lab)
    cal = await calibrate(owner_engine, lab, mission_id)  # the humans' own
    await measurement(owner_engine, lab, c, the_fish, cal, length_m=0.39)

    run = await _in(app_engine, lab, mission_automatic, mission_id)

    assert run.compared == 1
    assert run.median_error_pct == pytest.approx((0.42 - 0.39) / 0.39 * 100)
    assert run.within_5_pct == 0
    assert run.within_10_pct == 1
    assert calibration.compared == 0


async def test_a_missions_failures_are_listed(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    await exec_(
        owner_engine,
        "INSERT INTO automatic_results_failures (tenant_id, mission_id, step, error) "
        "VALUES (:t, :m, 'calibration', 'boom')",
        t=lab,
        m=mission_id,
    )

    run = await _in(app_engine, lab, mission_automatic, mission_id)

    assert [(f.step, f.error) for f in run.failures] == [("calibration", "boom")]


async def test_a_length_whose_calibration_is_no_longer_current_is_not_shown(
    owner_engine, app_engine
):
    """A newer accepted label-free fit replaces the one the length used: that
    length is stale until the run measures again."""
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _in(app_engine, lab, persist_automatic_calibration, mission_id, _label_free())

    assert (await _frame(app_engine, lab, c)).length_m is None


# -- head/tail v5: the SimCC points are the frame's head and tail ---------------------


async def _simcc(owner_engine, app_engine, lab, *, head, tail, status="predicted"):
    """A frame whose SAM rule is `_predicted`'s (1800,1500)-(2300,1480) and
    whose SimCC keypoints are `head` and `tail` (or refused): its mission
    and capture."""
    from fishsense_services_api.automatic_results_store import (
        AutomaticKeypointRow,
        automatic_keypoint_inputs,
        persist_automatic_keypoints,
    )

    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c), keypoints=False)
    (owed,) = await _in(app_engine, lab, automatic_keypoint_inputs, mission_id)
    await _in(app_engine, lab, persist_automatic_keypoints, mission_id, [
        AutomaticKeypointRow(prediction_id=owed.prediction_id, capture_id=c,
                             status=status, head=head, tail=tail, confidence=0.7,
                             checkpoint="simcc@test")])  # fmt: skip
    return mission_id, c


async def test_a_frame_with_simcc_keypoints_shows_them_as_its_head_and_tail(
    owner_engine, app_engine
):
    """The owner, 2026-10-26: v5 is what lengths are measured from, so it is
    what a reviewer sees and accepts. The SAM rule's points come too, named."""
    lab = await tenant(owner_engine)
    _, c = await _simcc(owner_engine, app_engine, lab, head=[1790.0, 1505.0],
                        tail=[2250.0, 1490.0])  # fmt: skip

    found = await _frame(app_engine, lab, c)

    assert (found.head, found.tail) == ((1790.0, 1505.0), (2250.0, 1490.0))
    assert found.head_tail_source == "simcc"
    assert found.keypoint_status == "predicted"
    assert found.keypoint_checkpoint == "simcc@test"
    assert (found.sam_head, found.sam_tail) == ((1800.0, 1500.0), (2300.0, 1480.0))


async def test_a_frame_sent_to_review_still_shows_simccs_points_and_says_why(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c = await _simcc(owner_engine, app_engine, lab, head=[1800.0, 1500.0],
                        tail=[2000.0, 1490.0])  # fmt: skip

    found = await _frame(app_engine, lab, c)

    assert found.head_tail_source == "simcc"
    assert found.keypoint_status == "keypoints_disagree"
    assert found.tail == (2000.0, 1490.0)
    assert found.keypoint_disagreement == pytest.approx(0.6, abs=0.01)


async def test_a_refused_box_or_no_keypoints_falls_back_to_the_sam_rule(
    owner_engine, app_engine
):
    """A fish model's frame never gets keypoints (0074); a refused box has
    none. Both show the SAM rule's points, named as such."""
    lab = await tenant(owner_engine)
    _, refused = await _simcc(
        owner_engine, app_engine, lab, head=None, tail=None, status="refused"
    )
    mission_id = await _backlog_mission(owner_engine, lab)
    model = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(model), keypoints=False)

    for c, status in ((refused, "keypoints_refused"), (model, None)):
        found = await _frame(app_engine, lab, c)
        assert (found.head, found.tail) == ((1800.0, 1500.0), (2300.0, 1480.0))
        assert found.head_tail_source == "sam_rule"
        assert found.keypoint_status == status


async def test_a_rerun_of_the_keypoints_shows_the_newest(owner_engine, app_engine):
    """Keypoints are append-only: a rerun on the same prediction is the one shown."""
    from fishsense_services_api.automatic_results_store import (
        AutomaticKeypointRow,
        persist_automatic_keypoints,
    )

    lab = await tenant(owner_engine)
    mission_id, c = await _simcc(owner_engine, app_engine, lab, head=[1790.0, 1505.0],
                                 tail=[2250.0, 1490.0])  # fmt: skip
    first = await _frame(app_engine, lab, c)
    await _in(app_engine, lab, persist_automatic_keypoints, mission_id, [
        AutomaticKeypointRow(prediction_id=first.prediction_id, capture_id=c,
                             status="predicted", head=[1795.0, 1502.0],
                             tail=[2280.0, 1485.0], confidence=0.8,
                             checkpoint="simcc@rerun")])  # fmt: skip

    found = await _frame(app_engine, lab, c)

    assert (found.head, found.tail) == ((1795.0, 1502.0), (2280.0, 1485.0))
    assert found.keypoint_checkpoint == "simcc@rerun"
