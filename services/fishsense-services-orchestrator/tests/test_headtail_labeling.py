"""Head/tail in Label Studio: the pre-annotations, the attach targets, the sync.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_headtail_populate_predictions.py (all of it),
test_backfill_headtail_predictions.py (all of it) and the task-reading half of
test_sync_headtail_labels_activity.py. Names, bodies and reasons are v1's; the
v2 adaptation is only that rows are keyed by capture, not image id.

Two keypoints per task, and both must carry what the project's XML declares
(`Snout` / `Fork` on `kp-1`): the sync reads `from_name == "kp-1"` back, so a
mismatch silently produces tasks whose labels never return.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fishsense_services_api.headtail_store import (
    CurrentHeadTailPrediction,
    LiveHeadTailLabel,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.headtail.labeling import (
    HEADTAIL_LABELING_CONFIG_XML,
    HEADTAIL_PROJECT_TITLE_SUFFIX,
    build_task,
    head_tail_sync_from_task,
    prediction_annotations,
    select_attach_targets,
    select_predicted_capture_ids,
)
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.populate import TaskImage

C1, C2, C9 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _prediction(
    capture_id=C1, head=(100.0, 200.0), tail=(300.0, 220.0), width=4000,
    height=3000, status="predicted", silhouette_ratio=0.25,
    rejected_low_confidence=False, predictor_version=2,
):  # fmt: skip
    return CurrentHeadTailPrediction(
        capture_id=capture_id,
        status=status,
        head_x=head[0],
        head_y=head[1],
        tail_x=tail[0],
        tail_y=tail[1],
        width=width,
        height=height,
        silhouette_ratio=silhouette_ratio,
        rejected_low_confidence=rejected_low_confidence,
        predictor_version=predictor_version,
    )


# -- the pre-annotation (v1's test_headtail_populate_predictions.py) ---------------


def test_emits_two_keypoints_labelled_snout_and_fork():
    out = prediction_annotations(_prediction())
    assert len(out) == 1
    results = out[0]["result"]
    assert [r["value"]["keypointlabels"][0] for r in results] == ["Snout", "Fork"]


def test_keypoints_use_the_projects_from_name():
    results = prediction_annotations(_prediction())[0]["result"]
    assert {r["from_name"] for r in results} == {"kp-1"}
    assert {r["to_name"] for r in results} == {"image"}
    assert {r["type"] for r in results} == {"keypointlabels"}


def test_the_labeling_config_declares_what_the_keypoints_and_sync_use():
    """The XML, the seeded keypoints and the sync must agree on `kp-1`,
    `image`, `Snout` and `Fork` -- three places, one vocabulary."""
    root = ET.fromstring(HEADTAIL_LABELING_CONFIG_XML)
    keypoints = root.find("KeyPointLabels")
    assert (keypoints.get("name"), keypoints.get("toName")) == ("kp-1", "image")
    assert [label.get("value") for label in keypoints] == ["Snout", "Fork"]
    assert root.find("Image").get("value") == "$image"
    assert HEADTAIL_PROJECT_TITLE_SUFFIX == "HeadTail Labeling"


def test_pixels_convert_to_percentages_using_the_recorded_dims():
    out = prediction_annotations(
        _prediction(
            head=(1000.0, 600.0), tail=(3000.0, 1500.0), width=4000, height=3000
        )
    )
    snout, fork = out[0]["result"]
    assert snout["value"]["x"] == pytest.approx(25.0)
    assert snout["value"]["y"] == pytest.approx(20.0)
    assert fork["value"]["x"] == pytest.approx(75.0)
    assert fork["value"]["y"] == pytest.approx(50.0)
    assert (snout["original_width"], snout["original_height"]) == (4000, 3000)


def test_no_annotation_for_an_abstention():
    assert not prediction_annotations(
        _prediction(status="no_detections", head=(None, None))
    )
    assert not prediction_annotations(None)


def test_no_annotation_when_frame_dims_are_missing():
    """Without dims the pixel->percentage conversion is undefined."""
    assert not prediction_annotations(_prediction(width=None))


def test_low_confidence_is_seeded_as_a_task_with_no_prediction():
    assert not prediction_annotations(_prediction(rejected_low_confidence=True))


def test_silhouette_band_rejects_a_non_fish_shape():
    """Applied at seed time, not predict time, so the band can be retuned from
    rows already collected without re-predicting anything."""
    assert not prediction_annotations(_prediction(silhouette_ratio=0.02))
    assert not prediction_annotations(_prediction(silhouette_ratio=0.9))
    assert prediction_annotations(_prediction(silhouette_ratio=0.25))


@pytest.mark.parametrize("ratio", [0.18, 0.32])
def test_the_band_is_inclusive(ratio):
    assert prediction_annotations(_prediction(silhouette_ratio=ratio))


def test_a_missing_ratio_is_not_treated_as_out_of_band():
    """None means "not recorded" and must not suppress older rows."""
    assert prediction_annotations(_prediction(silhouette_ratio=None))


def test_the_tag_is_the_rows_own_tier():
    """A fallback row tagged as SAM 3.1 would make the later upgrade look
    already attached, and the labeler would keep Mask R-CNN keypoints."""
    assert prediction_annotations(_prediction(predictor_version=-1))[0][
        "model_version"
    ] == ("v-1 crop=1800x1350")
    assert prediction_annotations(_prediction(predictor_version=2))[0][
        "model_version"
    ] == ("v2 crop=1800x1350")


def test_prediction_gate_only_admits_predicted_images():
    predictions = [_prediction(capture_id=C1), _prediction(C2, status="no_detections")]
    assert select_predicted_capture_ids(predictions) == {C1, C2}


def test_prediction_gate_counts_abstentions_as_predicted():
    """An abstention *is* a prediction attempt: holding it back from populate
    forever would strand it instead."""
    assert select_predicted_capture_ids(
        [_prediction(C9, status="headtail_failed")]
    ) == {C9}


def test_build_task_emits_dual_image_and_img_keys_and_the_prediction():
    """Prod configs read `image` or `img`; both are the located JPEG's URI."""
    ref = ObjectRef(
        bucket="labels", key="fishsense-lite/preprocess_headtail_jpeg/a.JPG"
    )
    image = TaskImage(number=7, image=ref, captured_at=datetime(2025, 1, 1, tzinfo=UTC))

    task = build_task(image, _prediction())

    assert task["data"]["image"] == task["data"]["img"] == ref.uri
    assert task["data"]["image_id"] == 7
    assert not task["annotations"]
    assert task["predictions"] == prediction_annotations(_prediction())
    assert not build_task(image, None)["predictions"]


# -- the backfill's targets (v1's test_backfill_headtail_predictions.py) -------------


def _label(capture_id, task_id=900, project_id=71, completed=False):
    return LiveHeadTailLabel(
        id=uuid.uuid4(), capture_id=capture_id, ls_project_id=project_id,
        ls_task_id=task_id, completed=completed,
    )  # fmt: skip


def test_selects_an_incomplete_task_with_a_placeable_prediction():
    assert select_attach_targets([_prediction(C1)], [_label(C1, task_id=900)]) == {
        C1: (900, 71)
    }


def test_skips_a_completed_task():
    """A labeler already placed those points; a fresh pre-annotation beside
    them is noise at best."""
    assert not select_attach_targets([_prediction(C1)], [_label(C1, completed=True)])


def test_skips_an_abstention():
    assert not select_attach_targets(
        [_prediction(C1, status="no_detections", head=(None, None))], [_label(C1)]
    )


def test_skips_a_task_with_no_ls_ids():
    assert not select_attach_targets([_prediction(C1)], [_label(C1, task_id=None)])
    assert not select_attach_targets(
        [_prediction(C1)], [_label(C1, task_id=None, project_id=None)]
    )


def test_skips_an_image_with_no_prediction():
    assert not select_attach_targets([], [_label(C1)])


def test_first_non_superseded_task_per_image_wins():
    """The labels are the live ones already (superseded rows never reach
    here); the first of them wins."""
    targets = select_attach_targets(
        [_prediction(C1)], [_label(C1, task_id=900), _label(C1, task_id=901)]
    )
    assert targets == {C1: (900, 71)}


# -- reading a task for the sync (v1's __update_headtail_label) ----------------------

_HOSTED_ANNOTATOR = {"user_id": 141592, "id": 141592, "username": "ccrutchf"}


def _kp(label, x, y, width=100, height=200):
    return {
        "from_name": "kp-1",
        "original_width": width,
        "original_height": height,
        "value": {"x": x, "y": y, "keypointlabels": [label]},
    }


def _task(result, annotators=(_HOSTED_ANNOTATOR,), is_labeled=True):
    return LabelStudioTask.from_sdk(
        SimpleNamespace(
            id=1,
            annotators=list(annotators),
            annotations=[{"result": result}] if result is not None else [],
            is_labeled=is_labeled,
            updated_at="2026-05-01T00:00:00Z",
        )
    )


def test_a_labeled_task_gives_both_points_in_pixels():
    sync = head_tail_sync_from_task(_task([_kp("Snout", 10, 20), _kp("Fork", 30, 40)]))

    assert sync.completed is True
    assert (sync.head_x, sync.head_y, sync.tail_x, sync.tail_y) == (
        10.0,
        40.0,
        30.0,
        80.0,
    )
    assert sync.ls_labeler_id == 141592, "hosted LS's dict annotator, unwrapped"
    assert sync.ls_updated_at == datetime(2026, 5, 1, tzinfo=UTC)


def test_only_kp_1_is_read():
    sync = head_tail_sync_from_task(
        _task([{**_kp("Snout", 10, 20), "from_name": "other"}, _kp("Fork", 30, 40)])
    )
    assert sync.head_x is None


@pytest.mark.parametrize("present", ["Snout", "Fork"])
def test_one_keypoint_alone_moves_neither(present):
    """v1 wrote the coordinates only when both were placed."""
    sync = head_tail_sync_from_task(_task([_kp(present, 10, 20)]))
    assert (sync.head_x, sync.head_y, sync.tail_x, sync.tail_y) == (None,) * 4


def test_the_heads_frame_dims_convert_both_points():
    """v1 converted both keypoints with the Snout section's dims."""
    sync = head_tail_sync_from_task(
        _task([_kp("Snout", 50, 50, 100, 200), _kp("Fork", 50, 50, 999, 999)])
    )
    assert (sync.tail_x, sync.tail_y) == (50.0, 100.0)
