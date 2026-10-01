"""Stage 14 (measure fish): the orchestrator's input to the processor, and back.

Ported from the shape of fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/src/
fishsense_data_processing_workflow_worker/activities/measure_fish_activity.py,
which took a bare `dive_id` and did its own SDK reads and writes. In v2 the
orchestrator decides what to measure -- which captures, and per capture the
one laser label and the one head/tail label -- and binds the result to a fish
when it persists it. The processor does only the geometry: the laser dot's
depth, and head-to-tail at that depth.

The result echoes each capture's inputs, so a refusal can be recorded against
exactly what was tried and retried only when one of them changes.

v2 changes:

* **a zero or non-finite length is a refusal, and it comes back** (PLAN.md
  §9.16). v1 dropped a NaN length and could write a zero one (head and tail on
  one pixel), which v2's `measurements` refuses (CHECK length_m > 0); either
  way the capture stayed in the cohort and the dive never drained;
* **the result names its algorithm, version and core**, which 0013 requires of
  every server measurement.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from fishsense_services_contracts.laser_depth import (
    CameraMatrix,
    FiniteFloat,
    LaserCalibrationGeometry,
    LaserDot,
)

__all__ = [
    "FishLength",
    "HeadTail",
    "MeasureFishCapture",
    "MeasureFishInput",
    "MeasureFishResult",
]


class HeadTail(BaseModel):
    """A valid head/tail label: both keypoints, in pixels."""

    head_tail_label_id: UUID
    head_x: FiniteFloat
    head_y: FiniteFloat
    tail_x: FiniteFloat
    tail_y: FiniteFloat


class MeasureFishCapture(BaseModel):
    capture_id: UUID
    #: The species label that made the capture measurable; the orchestrator
    #: binds the fish from it when it persists the length.
    species_label_id: UUID
    laser: LaserDot
    head_tail: HeadTail


class MeasureFishInput(BaseModel):
    dive_id: UUID
    camera_matrix: CameraMatrix
    calibration: LaserCalibrationGeometry
    captures: list[MeasureFishCapture]


class FishLength(BaseModel):
    """One capture's length, or why there is none."""

    capture_id: UUID
    species_label_id: UUID
    laser: LaserDot
    head_tail: HeadTail
    length_m: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None
    #: The laser dot's depth the length was taken at; None when not finite.
    depth_m: FiniteFloat | None
    refusal: Literal["non_finite_length", "zero_length"] | None

    @model_validator(mode="after")
    def _a_length_or_a_refusal(self) -> Self:
        if (self.length_m is None) == (self.refusal is None):
            raise ValueError("exactly one of length_m and refusal")
        return self


class MeasureFishResult(BaseModel):
    dive_id: UUID
    algorithm: str = Field(min_length=1)
    algorithm_version: str = Field(min_length=1)
    #: The fishsense-core wheel that computed it.
    core_version: str = Field(min_length=1)
    captures: list[FishLength]
