"""A processor worker's refusals: the Temporal error types that are the
worker's fault, never a frame's or a mission's.

New in v2 (2026-10-24). A worker with no GPU for SAM 3.1, or whose model
weights cannot load, refuses every frame alike, and no retry helps: the
pod's settings, the bytes in Garage's `model-weights`, or core's manifest
must change first. So a processor workflow that leaves a failing frame out
(or records it ``failed``) fails the whole run on a refusal instead, and the
orchestrator's automatic-results drain records nothing against the dive or
its frames and stops: charged to the frames, a broken pod cost every frame
of the mission its tries, and a ``frame_failed`` at three runs (0044).

A weights failure is ``{model}{kind}``: one type per model and cause, raised
non-retryably by the processor's `weights.refuse_unloadable_weights`. The
names that were already recorded before this module (``Sam3*``,
``SlateDetector*``, ``Bioclip*``) are kept. Both sides read the names from
here, so neither can add one the other does not know.

Not a payload: no contract version.
"""

from __future__ import annotations

__all__ = [
    "AUTOMATIC_FRAME_REFUSALS",
    "BIOCLIP",
    "HEADTAIL_KEYPOINTS",
    "KEYPOINT_REFUSALS",
    "LASER_DETECTOR",
    "NO_GPU_FOR_KEYPOINTS",
    "NO_GPU_FOR_SAM3",
    "SAM3",
    "SAM3_REFUSALS",
    "SLATE_DETECTOR",
    "WEIGHTS_FAILURE_KINDS",
    "WORKER_REFUSALS",
    "weights_failure_types",
]

#: Why weights cannot load, as the processor's mapping names each cause:
#: the settings are missing or invalid; the bytes are not the pinned file;
#: the object is not in `model-weights`; the manifest has no such entry.
WEIGHTS_FAILURE_KINDS = (
    "SettingsInvalid",
    "WeightsCorrupt",
    "WeightsUnavailable",
    "NotInManifest",
)

SAM3 = "Sam3"
LASER_DETECTOR = "LaserDetector"
HEADTAIL_KEYPOINTS = "HeadtailKeypoints"
SLATE_DETECTOR = "SlateDetector"
BIOCLIP = "Bioclip"

NO_GPU_FOR_SAM3 = "NoGpuForSam3"
NO_GPU_FOR_KEYPOINTS = "NoGpuForKeypoints"


def weights_failure_types(model: str) -> tuple[str, ...]:
    """Every weights failure type of ``model`` (e.g. ``Sam3WeightsCorrupt``)."""
    return tuple(f"{model}{kind}" for kind in WEIGHTS_FAILURE_KINDS)


#: A SAM 3.1 stage's (head/tail predict, mask backfill, slate pose, shadow
#: segmentation).
SAM3_REFUSALS = (NO_GPU_FOR_SAM3, *weights_failure_types(SAM3))
#: The automatic frames': SAM 3.1, and the laser detector for the dot.
AUTOMATIC_FRAME_REFUSALS = (*SAM3_REFUSALS, *weights_failure_types(LASER_DETECTOR))
#: The SimCC keypoint replay's.
KEYPOINT_REFUSALS = (NO_GPU_FOR_KEYPOINTS, *weights_failure_types(HEADTAIL_KEYPOINTS))

#: Every refusal a processor worker raises.
WORKER_REFUSALS = frozenset(
    (
        *AUTOMATIC_FRAME_REFUSALS,
        *KEYPOINT_REFUSALS,
        *weights_failure_types(SLATE_DETECTOR),
        "SlateDetectorCheckpointInvalid",
        *weights_failure_types(BIOCLIP),
    )
)
