"""Head/tail in Label Studio: the project's config, its tasks, and reading them.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/:
create_headtail_label_studio_project_activity.py (the title suffix and the
labeling config), populate_headtail_label_studio_project_activity.py
(`prediction_annotations`, `select_predicted_image_ids`, `_build_task`),
backfill_headtail_predictions_activity.py (`select_attach_targets`) and
sync_headtail_labels_for_label_studio_project_activity.py
(`__update_headtail_label`, as a pure reading of a task). Behaviour is v1's.

The config, the seeded keypoints and the sync share one vocabulary:
`KeyPointLabels name="kp-1" toName="image"` with `Snout` and `Fork`. A mismatch
seeds tasks whose labels never come back.

v2 changes: rows are keyed by capture (v1: image id); a task's image is the
JPEG the object store located (`TaskImage`), never a URL built here.
"""

from __future__ import annotations

import uuid
from typing import Iterable, Mapping

from fishsense_services_api.headtail_store import (
    CurrentHeadTailPrediction,
    LiveHeadTailLabel,
)
from fishsense_services_api.label_sync_store import HeadTailSync
from fishsense_services_contracts.headtail import headtail_model_version_tag
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.populate import TaskImage, build_task_data

__all__ = [
    "HEADTAIL_LABELING_CONFIG_XML",
    "HEADTAIL_PROJECT_TITLE_SUFFIX",
    "MAX_SILHOUETTE_RATIO",
    "MIN_SILHOUETTE_RATIO",
    "build_task",
    "head_tail_sync_from_task",
    "prediction_annotations",
    "select_attach_targets",
    "select_predicted_capture_ids",
]

#: The per-dive project's title is `{dive.name} #{number} - HeadTail Labeling`.
HEADTAIL_PROJECT_TITLE_SUFFIX = "HeadTail Labeling"

#: The prod head/tail project's labeling config (v1's constant, verbatim).
HEADTAIL_LABELING_CONFIG_XML = """\
<View>
  <KeyPointLabels name="kp-1" toName="image">
    <Label value="Snout" background="#FFA39E"/>
    <Label value="Fork" background="#26a269"/>
  </KeyPointLabels>
  <Image name="image" value="$image" zoom="true" zoomControl="true"/>
</View>
"""

_KEYPOINT_FROM_NAME = "kp-1"
_KEYPOINT_TO_NAME = "image"
_SNOUT_LABEL = "Snout"
_FORK_LABEL = "Fork"

#: A real fish silhouette's area / length**2 runs ~0.15-0.30. Applied at seed
#: time, not in the predictor, so it can be retuned against rows already
#: collected. Keeping 0.18-0.32 drops ~24% of predictions and moves p90 length
#: error from 17.1% to 12.7% (v1's measurement).
MIN_SILHOUETTE_RATIO = 0.18
MAX_SILHOUETTE_RATIO = 0.32


def select_predicted_capture_ids(
    predictions: Iterable[CurrentHeadTailPrediction],
) -> set[uuid.UUID]:
    """Captures the detector has visited, abstentions included.

    Populate is prediction-gated: it seeds rows, and the predict cohort
    requires "no live label", so populating first would starve an image of a
    prediction forever. An abstention counts -- holding it back would strand
    the image with neither a prediction nor a human label.
    """
    return {p.capture_id for p in predictions}


def prediction_annotations(prediction: CurrentHeadTailPrediction | None) -> list:
    """The Label Studio `predictions` for a task -- two keypoints -- or []
    when there is nothing placeable (an abstention, a low-confidence or
    out-of-band shape, or no frame dims to convert by). [] still creates the
    task; it arrives unseeded."""
    if prediction is None:
        return []
    if prediction.rejected_low_confidence or prediction.status != "predicted":
        return []
    head_x, head_y = prediction.head_x, prediction.head_y
    tail_x, tail_y = prediction.tail_x, prediction.tail_y
    width, height = prediction.width, prediction.height
    if None in (head_x, head_y, tail_x, tail_y) or not width or not height:
        return []
    ratio = prediction.silhouette_ratio
    # None means "not recorded", not "out of band".
    if ratio is not None and not MIN_SILHOUETTE_RATIO <= ratio <= MAX_SILHOUETTE_RATIO:
        return []

    def _point(x, y, label):
        return {
            "from_name": _KEYPOINT_FROM_NAME,
            "to_name": _KEYPOINT_TO_NAME,
            "type": "keypointlabels",
            "original_width": width,
            "original_height": height,
            "image_rotation": 0,
            "value": {
                "x": x / width * 100,
                "y": y / height * 100,
                "width": 0.5,
                "keypointlabels": [label],
            },
        }

    return [
        {
            # The row's own tier: the backfill dedupes on (task, tag).
            "model_version": headtail_model_version_tag(prediction.predictor_version),
            "result": [
                _point(head_x, head_y, _SNOUT_LABEL),
                _point(tail_x, tail_y, _FORK_LABEL),
            ],
        }
    ]


def build_task(
    image: TaskImage, prediction: CurrentHeadTailPrediction | None = None
) -> dict:
    """A Label Studio task for one capture's stage-5.1 JPEG, seeded with its
    prediction when there is one to place."""
    return {
        "data": build_task_data(image),
        "predictions": prediction_annotations(prediction),
        "annotations": [],
    }


def select_attach_targets(
    predictions: Iterable[CurrentHeadTailPrediction],
    labels: Iterable[LiveHeadTailLabel],
) -> dict[uuid.UUID, tuple[int, int]]:
    """`capture -> (task, project)` for the backfill: a placeable prediction
    (populate's own rules, so the two paths cannot drift) and an incomplete
    live task. The first such task per capture wins."""
    placeable = {p.capture_id for p in predictions if prediction_annotations(p)}
    targets: dict[uuid.UUID, tuple[int, int]] = {}
    for label in labels:
        if label.completed:
            continue
        if label.ls_task_id is None or label.ls_project_id is None:
            continue
        if label.capture_id not in placeable or label.capture_id in targets:
            continue
        targets[label.capture_id] = (int(label.ls_task_id), int(label.ls_project_id))
    return targets


def _section(sections: list[Mapping], label: str) -> Mapping | None:
    return next((s for s in sections if s["value"]["keypointlabels"][0] == label), None)


def head_tail_sync_from_task(task: LabelStudioTask) -> HeadTailSync:
    """What an annotated task says about its head/tail label.

    The first annotation's `kp-1` keypoints, Snout as the head and Fork as the
    tail, from Label Studio's percentages to pixels with the Snout section's
    frame dims (v1's). The points only when both are there.
    """
    head_x = head_y = tail_x = tail_y = None
    if task.annotations:
        sections = [
            r
            for r in task.annotations[0].get("result", [])
            if r.get("from_name") == _KEYPOINT_FROM_NAME
        ]
        head = _section(sections, _SNOUT_LABEL)
        tail = _section(sections, _FORK_LABEL)
        if head is not None and tail is not None:
            width, height = head["original_width"], head["original_height"]
            head_x = head["value"]["x"] * width / 100
            head_y = head["value"]["y"] * height / 100
            tail_x = tail["value"]["x"] * width / 100
            tail_y = tail["value"]["y"] * height / 100
    return HeadTailSync(
        completed=task.is_labeled,
        head_x=head_x,
        head_y=head_y,
        tail_x=tail_x,
        tail_y=tail_y,
        ls_labeler_id=task.annotator_id,
        ls_updated_at=task.updated_at,
        ls_payload=task.payload,
    )
