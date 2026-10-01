"""What a laser task carries: its colour, its pre-annotation, its annotation.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_populate_laser_label_studio_project_activity.py (the pure half:
the dive-majority colour, the keypoint, the prediction and the auto-accepted
annotation, the task). Names and reasons are v1's. v2 adaptations: a task's
image is the located JPEG (`TaskImage`), never a URL built from settings; the
service account comes from ``FISHSENSE_LABEL_STUDIO_BOT_USER_ID``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fishsense_services_contracts.laser import laser_model_version_tag
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.labels.populate import TaskImage
from fishsense_services_orchestrator.laser.annotations import (
    LASER_LABELING_CONFIG_XML,
    LASER_PROJECT_TITLE_SUFFIX,
    Dot,
    auto_accepted_annotations,
    build_laser_task,
    dive_laser_label,
    prediction_annotations,
)

DOT = Dot(x=2000.0, y=1500.0, width=4000, height=3000)
IMAGE = TaskImage(
    number=132158,
    image=ObjectRef(bucket="labels", key="fishsense-lite/preprocess_jpeg/abc.JPG"),
    captured_at=datetime(2025, 3, 6, 17, 0, tzinfo=UTC),
)


# -- the dive's colour -------------------------------------------------------------


def test_unanimous_green_dive_is_labelled_green():
    assert dive_laser_label(["green"] * 5) == "Green Laser"


def test_a_single_misread_frame_does_not_split_the_dive():
    assert dive_laser_label(["green"] * 9 + ["red"]) == "Green Laser"


def test_abstentions_do_not_count_as_votes():
    assert dive_laser_label([None, None, None, "green"]) == "Green Laser"


def test_no_votes_falls_back_to_the_more_common_colour():
    assert dive_laser_label([]) == "Red Laser"
    assert dive_laser_label([None]) == "Red Laser"


def test_a_tie_falls_back_rather_than_picking_arbitrarily():
    assert dive_laser_label(["red", "green"]) == "Red Laser"


# -- the pre-annotation ------------------------------------------------------------


def test_prediction_annotations_converts_pixels_to_percent():
    (wrapper,) = prediction_annotations(DOT, "Red Laser")

    assert wrapper["model_version"] == laser_model_version_tag()
    (result,) = wrapper["result"]
    assert result["value"]["x"] == 50.0 and result["value"]["y"] == 50.0
    assert result["value"]["width"] == 0.5
    assert (result["from_name"], result["to_name"]) == ("laser", "img")
    assert (result["original_width"], result["original_height"]) == (4000, 3000)


def test_prediction_annotations_empty_for_none_or_missing_dims():
    assert prediction_annotations(None, "Red Laser") == []
    assert prediction_annotations(Dot(None, None, 4000, 3000), "Red Laser") == []
    assert prediction_annotations(Dot(1.0, 2.0, None, 3000), "Red Laser") == []


def test_the_chosen_label_reaches_the_keypoint_annotation():
    (wrapper,) = prediction_annotations(DOT, "Green Laser")

    assert wrapper["result"][0]["value"]["keypointlabels"] == ["Green Laser"]


# -- the auto-accepted annotation -----------------------------------------------------


def test_auto_accepted_prediction_becomes_an_annotation_not_a_prediction():
    task = build_laser_task(IMAGE, DOT, "Red Laser", auto_accept=True, bot_user_id=0)

    assert task["annotations"] and task["predictions"] == []


def test_auto_accepted_annotation_matches_the_shape_of_an_accepted_prediction():
    """`origin: prediction` is what Label Studio stamps when a labeler submits
    a pre-annotation unchanged -- what 93% of reviews produced."""
    (annotation,) = auto_accepted_annotations(DOT, "Red Laser", bot_user_id=0)
    (prediction,) = prediction_annotations(DOT, "Red Laser")

    (result,) = annotation["result"]
    assert result["origin"] == "prediction"
    assert {k: v for k, v in result.items() if k != "origin"} == prediction["result"][0]


def test_auto_accepted_annotation_names_the_service_account():
    """Imported annotations are attributed to the project owner otherwise."""
    (annotation,) = auto_accepted_annotations(DOT, "Red Laser", bot_user_id=4242)

    assert annotation["completed_by"] == 4242


def test_auto_accepted_annotation_omits_completed_by_when_unconfigured():
    """`completed_by: null` is an LS validation error that would fail a
    whole dive's populate; the old (wrong) attribution is survivable."""
    (annotation,) = auto_accepted_annotations(DOT, "Red Laser", bot_user_id=0)

    assert "completed_by" not in annotation


def test_auto_accepted_annotation_is_not_marked_ground_truth():
    """Left unset, Label Studio stamps it true; dive 520 carried it on all 37."""
    (annotation,) = auto_accepted_annotations(DOT, "Red Laser", bot_user_id=0)

    assert annotation["ground_truth"] is False


def test_a_prediction_the_gate_did_not_clear_is_still_seeded_for_review():
    task = build_laser_task(IMAGE, DOT, "Red Laser", auto_accept=False, bot_user_id=0)

    assert task["annotations"] == []
    assert task["predictions"] == prediction_annotations(DOT, "Red Laser")


def test_the_task_points_at_the_located_jpeg_with_both_keys():
    task = build_laser_task(IMAGE, DOT, "Red Laser", auto_accept=False, bot_user_id=0)

    uri = "s3://labels/fishsense-lite/preprocess_jpeg/abc.JPG"
    assert task["data"]["image"] == task["data"]["img"] == uri
    assert task["data"]["image_id"] == 132158


# -- the project -------------------------------------------------------------------


def test_the_project_is_v1s():
    """Titles `{name} #{number} - Laser Calibration Labeling`, so every v1
    project is still found; the keypoint control is `laser`, which the sync
    reads."""
    assert LASER_PROJECT_TITLE_SUFFIX == "Laser Calibration Labeling"
    assert '<KeyPointLabels name="laser" toName="img">' in LASER_LABELING_CONFIG_XML
    assert 'value="Green Laser"' in LASER_LABELING_CONFIG_XML
