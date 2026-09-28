"""Laser depth: the orchestrator's input to the processor, and its result.

Ported from the shape of fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/src/
fishsense_data_processing_workflow_worker/activities/
compute_laser_depths_activity.py, which took a bare `dive_id` and did its own
SDK reads (the dive's camera intrinsics, its resolved extrinsics, its laser
labels) and writes. v2's processor never touches the database, so the
orchestrator resolves those inputs and persists the result:

* in: the camera matrix, the dive's effective laser calibration, and per
  capture the valid laser labels still worth trying, **in the order to try
  them** (v1: ascending label id; the first that triangulates in front of the
  camera wins);
* out: per capture, the depth (Z, used by stage 14), the range (the Euclidean
  norm; never conflate the two) and the triangulation's residual, naming the
  label it came from -- or, when no label triangulates in front of the camera,
  a **refusal** for each label tried.

v2 change: v1 counted that last case (`skipped_invalid_geometry`) and wrote
nothing, so the image stayed "needing depth" and the dive was re-selected
every hour forever (dive 32, PLAN.md §4.5/§9.16). v2 records the refusal, and
the cohort skips what it has tried.

Shared by `measurement`: the calibration geometry and a laser dot are the
same inputs there.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "CameraMatrix",
    "ComputeLaserDepthsInput",
    "ComputeLaserDepthsResult",
    "FiniteFloat",
    "LaserCalibrationGeometry",
    "LaserDepth",
    "LaserDepthCapture",
    "LaserDepthOutcome",
    "LaserDepthRefusal",
    "LaserDot",
    "Vector3",
]

#: A number JSON can carry: NaN and infinity are refused rather than turned
#: into something else on the way across.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
Vector3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
#: A pinhole camera matrix K, row-major. Only K: v1's projection uses
#: inv(camera_matrix) and no distortion, and v2 keeps that.
CameraMatrix = tuple[Vector3, Vector3, Vector3]


class LaserCalibrationGeometry(BaseModel):
    """The laser calibration a dive is measured with (its *effective* one)."""

    laser_calibration_id: UUID
    laser_position: Vector3
    #: A direction; fishsense-core normalises it and refuses a zero one.
    laser_axis: Vector3


class LaserDot(BaseModel):
    """A valid laser label: where the labeler put the dot, in pixels."""

    laser_label_id: UUID
    x: FiniteFloat
    y: FiniteFloat


class LaserDepthCapture(BaseModel):
    capture_id: UUID
    #: In the order to try them. Never empty: the orchestrator sends only
    #: captures with a label worth trying.
    laser_labels: list[LaserDot] = Field(min_length=1)


class ComputeLaserDepthsInput(BaseModel):
    dive_id: UUID
    camera_matrix: CameraMatrix
    calibration: LaserCalibrationGeometry
    captures: list[LaserDepthCapture]


class LaserDepth(BaseModel):
    """Where the laser dot was, in metres, in camera coordinates."""

    laser_label_id: UUID
    #: The dot it was computed at, echoed as a refusal echoes its dot. Label
    #: Studio sync moves a dot in place, keeping the label id, so the
    #: orchestrator writes the depth only if the label still sits here.
    x: FiniteFloat
    y: FiniteFloat
    #: The Z component: in front of the camera, or it is a refusal.
    depth_m: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    #: The Euclidean norm; longer than the depth off the optical axis.
    range_m: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    #: How close the camera ray and the laser ray came. Recorded, never gated
    #: on: it is blind along the laser's epipolar line. None when non-finite.
    residual_m: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


class LaserDepthRefusal(BaseModel):
    """A label that gave no depth in front of the camera, and what was tried."""

    laser_label_id: UUID
    x: FiniteFloat
    y: FiniteFloat
    reason: Literal["non_finite_depth", "non_positive_depth"]
    #: What the triangulation said; None when it was not finite.
    depth_m: FiniteFloat | None


class LaserDepthOutcome(BaseModel):
    capture_id: UUID
    #: None when every label was refused.
    depth: LaserDepth | None
    #: Every label that was tried and gave no depth, in the order tried.
    refusals: list[LaserDepthRefusal]


class ComputeLaserDepthsResult(BaseModel):
    dive_id: UUID
    #: The fishsense-core wheel that computed it; laser_depths requires it.
    core_version: str = Field(min_length=1)
    captures: list[LaserDepthOutcome]
