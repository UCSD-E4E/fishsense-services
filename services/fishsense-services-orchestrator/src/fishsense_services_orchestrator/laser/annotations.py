"""What a laser Label Studio task carries: the dive's colour, the model's
pre-annotation, or -- for a frame the gate cleared -- a finished annotation.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
src/fishsense_api_workflow_worker/activities/
populate_laser_label_studio_project_activity.py (`dive_laser_label`,
`_keypoint_result`, `_prediction_annotations`, `_auto_accepted_annotations`,
`_build_task`) and create_laser_label_studio_project_activity.py (the title
suffix and the labeling config). The single definition of dot, colour and
annotation shape, shared by populate, the prediction backfill and the
auto-accept apply, so they can never disagree about where the dot is.

v2 changes: the task's image is the JPEG the object store located (a migrated
frame's is v1's key, so its URL matches the task v1 imported); the service
account is passed in rather than read from global settings.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from fishsense_services_contracts.laser import laser_model_version_tag
from fishsense_services_orchestrator.labels.populate import TaskImage, build_task_data

__all__ = [
    "DEFAULT_LASER_LABEL",
    "LASER_LABELING_CONFIG_XML",
    "LASER_PROJECT_TITLE_SUFFIX",
    "Dot",
    "auto_accepted_annotations",
    "build_laser_task",
    "dive_laser_label",
    "prediction_annotations",
]

LASER_PROJECT_TITLE_SUFFIX = "Laser Calibration Labeling"

# The prod laser project's labeling config. The keypoint `from_name` is
# "laser", aligned with the sync's `LASER_LABEL_KEY_NAMES`.
LASER_LABELING_CONFIG_XML = """\
<View>
  <KeyPointLabels name="laser" toName="img">
    <Label value="Red Laser" background="#FFDF20"/>
    <Label value="Green Laser" background="#A684FF"/>
  </KeyPointLabels>
  <Image name="img" value="$image" zoom="true" zoomControl="true"/>
</View>
"""

_KEYPOINT_FROM_NAME = "laser"
_KEYPOINT_TO_NAME = "img"

# Colour is a property of the rig for a whole dive, so the per-frame colours
# the detector reads are votes and the majority labels every pre-annotation.
# Measured over 332 human-labelled dots: 98.48% per frame, 4 of 4 dives by
# majority. "Red Laser" -- the more common -- when there are no votes or a tie.
DEFAULT_LASER_LABEL = "Red Laser"
_LASER_LABEL_BY_COLOR = {"red": "Red Laser", "green": "Green Laser"}


@dataclass(frozen=True)
class Dot:
    """A prediction's dot in rectified pixels, and the frame it is in."""

    x: float | None
    y: float | None
    width: int | None
    height: int | None


def dive_laser_label(colors: Iterable[str | None]) -> str:
    """The keypoint label for every task of the dive: the majority of the
    per-frame colours, computed over all the dive's predictions so a top-up
    run agrees with the first."""
    votes = Counter(c for c in colors if c in _LASER_LABEL_BY_COLOR)
    if not votes or votes.get("red", 0) == votes.get("green", 0):
        return DEFAULT_LASER_LABEL
    winner, _ = votes.most_common(1)[0]
    return _LASER_LABEL_BY_COLOR[winner]


def _keypoint_result(dot: Dot | None, laser_label: str) -> dict | None:
    """The keypoint result item, or None when there is nothing placeable (no
    detection, or missing frame dims)."""
    if dot is None or dot.x is None or dot.y is None:
        return None
    if not dot.width or not dot.height:
        return None
    return {
        "from_name": _KEYPOINT_FROM_NAME,
        "to_name": _KEYPOINT_TO_NAME,
        "type": "keypointlabels",
        "original_width": dot.width,
        "original_height": dot.height,
        "image_rotation": 0,
        "value": {
            # Label Studio keypoints are percentages of the recorded frame.
            "x": dot.x / dot.width * 100,
            "y": dot.y / dot.height * 100,
            "width": 0.5,
            "keypointlabels": [laser_label],
        },
    }


def prediction_annotations(dot: Dot | None, laser_label: str) -> list:
    """The `predictions` list (one keypoint pre-annotation), or []."""
    result = _keypoint_result(dot, laser_label)
    if result is None:
        return []
    return [{"model_version": laser_model_version_tag(), "result": [result]}]


def auto_accepted_annotations(
    dot: Dot | None, laser_label: str, *, bot_user_id: int
) -> list:
    """The `annotations` list for a frame the gate cleared, or [].

    `origin: prediction` is what Label Studio stamps when a labeler submits a
    pre-annotation unchanged -- the outcome this stands in for. `ground_truth`
    is explicitly False: this skipped review, and left unset Label Studio
    stamps it true (prod dive 520). `completed_by` names the service account,
    because an imported annotation is otherwise attributed to the project's
    owner; it is omitted, not null, when unconfigured (null is a validation
    error that would fail the dive's populate).

    The label row's x/y are not written here: the sync stays their single
    writer, reading them back out of Label Studio as for a human annotation.
    """
    result = _keypoint_result(dot, laser_label)
    if result is None:
        return []
    annotation = {"result": [dict(result, origin="prediction")], "ground_truth": False}
    if bot_user_id:
        annotation["completed_by"] = int(bot_user_id)
    return [annotation]


def build_laser_task(
    image: TaskImage,
    dot: Dot | None,
    laser_label: str,
    *,
    auto_accept: bool,
    bot_user_id: int,
) -> dict:
    """One Label Studio task for the capture's JPEG. A frame the gate cleared
    is imported already annotated and NOT also as a prediction -- a task never
    carries both, or a labeler is shown a pre-annotation for finished work."""
    annotations = (
        auto_accepted_annotations(dot, laser_label, bot_user_id=bot_user_id)
        if auto_accept
        else []
    )
    return {
        "data": build_task_data(image),
        "annotations": annotations,
        "predictions": [] if annotations else prediction_annotations(dot, laser_label),
    }
