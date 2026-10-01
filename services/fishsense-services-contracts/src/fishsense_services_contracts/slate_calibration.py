"""Slates and laser calibration: what the orchestrator and processor exchange.

Informed by fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
preprocess_contracts.py (`PreprocessSlateImagesInput`,
`CheckerboardCalibrationImage`, `PerformCheckerboardCalibrationInput`,
`CheckerboardObservation`, `VerifyCheckerboardLatticeInput`,
`CheckerboardLatticeRender`) and the stage-13 activity's own SDK reads, which
had no contract (perform_laser_calibration_activity.py).

Four stages:

* **stage 9** (per-image): composite the slate template beside each rectified
  slate frame, for the labeler;
* **stage 13** (light): fit the laser from the labeled slate frames, behind
  v1's four gates;
* **checkerboard calibration** (per-image): the same fit, from a board the
  processor finds in the raw frames;
* **lattice verification** (per-image, on demand): draw what the detector
  found, for a person to judge.

v2 changes:

* ids are UUIDs, and every object the processor reads or writes is an
  ``ObjectRef`` the orchestrator issued -- never a checksum it builds a key
  from (PLAN.md §9.11);
* **stage 13 gets a contract.** v1's child took a bare dive id and did its
  own SDK reads and writes; v2's processor has no database, so the
  orchestrator resolves the observations and the dive's laser dots and the
  processor returns a `LaserCalibrationResult` -- accepted or refused, in the
  shapes `laser_calibrations` checks -- for the orchestrator to persist. The
  checkerboard fit returns the same result, for the same reason;
* a checkerboard target carries its pitch per axis (PLAN.md §4.3; v1: one
  `square_size_m`).

Integration adds `SLATE_CALIBRATION_MODELS` to the published schema.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "SLATE_CALIBRATION_MODELS",
    "CheckerboardCalibrationImage",
    "CheckerboardLatticeRender",
    "CheckerboardObservation",
    "CheckerboardTarget",
    "LaserCalibrationResult",
    "LatticeImage",
    "PerformCheckerboardCalibrationInput",
    "Point",
    "PreprocessSlateImage",
    "PreprocessSlateImagesInput",
    "SlateCalibrationInput",
    "SlateObservation",
    "VerifyCheckerboardLatticeInput",
]

#: A pixel, `(x, y)`.
Point = tuple[float, float]


# --- stage 9 ------------------------------------------------------------------


class PreprocessSlateImage(BaseModel):
    """One slate frame: where its raw is staged, and where its composite goes."""

    capture_id: UUID
    raw: ObjectRef
    #: The composite's target: over the JPEG where it already is (a migrated
    #: frame's v1 key, which Label Studio tasks hold), else the tenant's key.
    jpeg: ObjectRef


class PreprocessSlateImagesInput(BaseModel):
    """v1's `PreprocessSlateImagesInput`, with refs for its checksums."""

    dive_id: UUID
    slate_template_id: UUID
    #: The template PDF, staged to scratch from the NAS.
    slate_pdf: ObjectRef
    slate_dpi: PositiveInt
    #: The template's reference points, in PDF pixels at `slate_dpi`.
    reference_points: list[Point]
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    images: list[PreprocessSlateImage]


# --- stage 13 -----------------------------------------------------------------


class SlateObservation(BaseModel):
    """One live slate label on a frame carrying a live laser dot."""

    capture_id: UUID
    #: The labeler's clicked points, in rectified-photo pixels. None is kept
    #: (prod dive 526 holds JSON nulls) so the processor skips the *label*.
    reference_points: list[Point] | None
    #: 0-based indices into the template that the labeler could not place.
    skipped_points: list[int] | None
    laser_x: float
    laser_y: float


class SlateCalibrationInput(BaseModel):
    """Everything stage 13's fit reads, resolved by the orchestrator."""

    dive_id: UUID
    camera_matrix: list[list[float]]
    #: The slate template's points, in PDF pixels at `dpi`.
    template_points: list[Point]
    dpi: PositiveInt
    observations: list[SlateObservation]
    #: Every live laser dot in the dive (non-superseded, x/y set; incomplete
    #: and non-canonical included, as v1's `get_laser_labels`), for the gate
    #: that asks whether the fit describes the dive it will measure.
    dive_dots: list[Point]


class LaserCalibrationResult(BaseModel):
    """What a fit produced: an accepted calibration, or a recorded refusal."""

    outcome: Literal["accepted", "refused"]
    #: Where the ray crosses the camera's z=0 plane, z padded to 0 (v1).
    laser_position: list[float] | None = None
    laser_axis: list[float] | None = None
    #: v1's ApplicationError type (InsufficientLaserPoints,
    #: CalibrationImplausibleError, ...), so the parent can raise it again.
    refusal_type: str | None = None
    refusal_reason: str | None = None
    #: Usable observations the fit was offered, before the outlier trim.
    observation_count: NonNegativeInt
    observations_trimmed: NonNegativeInt
    #: The z-span of the fitted observations: what fixes the ray's direction.
    lever_arm_m: float | None = None
    #: Gate name -> "passed" | "abstained" | "refused" | "not_run".
    gate_verdicts: dict[str, str]
    core_version: str

    @model_validator(mode="after")
    def _shape_of_the_outcome(self) -> "LaserCalibrationResult":
        if self.outcome == "accepted":
            for name in ("laser_position", "laser_axis"):
                vector = getattr(self, name)
                if vector is None or len(vector) != 3:
                    raise ValueError(f"an accepted calibration needs a 3-vector {name}")
        elif not (self.refusal_type and self.refusal_reason):
            raise ValueError("a refusal needs its type and its reason")
        return self


# --- checkerboard -------------------------------------------------------------


class CheckerboardTarget(BaseModel):
    """A planar board, as its current `calibration_targets` version measures it.

    `rows` / `cols` are **interior** corners (15 x 11 squares -> 14 x 10), an
    upper bound on what may be detected. `pitch_x_m` spaces corners along a
    row (across the `cols` axis), `pitch_y_m` down a column. A value: frozen.
    """

    model_config = ConfigDict(frozen=True)

    rows: PositiveInt
    cols: PositiveInt
    pitch_x_m: PositiveFloat
    pitch_y_m: PositiveFloat


class CheckerboardCalibrationImage(BaseModel):
    """One canonical frame with a live laser dot (lowest label wins, v1)."""

    capture_id: UUID
    raw: ObjectRef
    laser_x: float
    laser_y: float


class PerformCheckerboardCalibrationInput(BaseModel):
    dive_id: UUID
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    target: CheckerboardTarget
    images: list[CheckerboardCalibrationImage]
    #: As in `SlateCalibrationInput`. v1's fit read them in the activity,
    #: before its try, so a transport error stayed retryable; v2 resolves them
    #: with the rest, so the processor has nothing to fetch at all.
    dive_dots: list[Point]


class CheckerboardObservation(BaseModel):
    """One frame's dot in camera space, or why the frame could not say."""

    capture_id: UUID
    #: `[x, y, z]` in metres; None when the frame is unusable.
    point: list[float] | None
    laser_x: float
    laser_y: float
    detected_rows: int | None = None
    detected_cols: int | None = None
    #: no_usable_board | dot_off_board | no_pose | no_ray_plane_intersection
    skip_reason: str | None = None


# --- lattice verification -----------------------------------------------------


class LatticeImage(BaseModel):
    capture_id: UUID
    raw: ObjectRef
    #: Where the rendered lattice is written. Its own folder: a render keyed
    #: like the stage-0.1 JPEG would overwrite the frame a laser task serves.
    render: ObjectRef
    laser_x: float
    laser_y: float


class VerifyCheckerboardLatticeInput(BaseModel):
    dive_id: UUID
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    target: CheckerboardTarget
    images: list[LatticeImage]
    #: The head of the list, not a random sample (v1): a stable subset keeps a
    #: re-run's verdicts comparable. None (v1's default here; the study's
    #: parent defaults to 20) lifts the cap.
    sample_limit: PositiveInt | None = None


class CheckerboardLatticeRender(BaseModel):
    """What was drawn for one frame, or why nothing was."""

    capture_id: UUID
    #: Where the render was written; None when the frame had no lattice.
    image: ObjectRef | None = None
    detected_rows: int | None = None
    detected_cols: int | None = None
    median_spacing_px: float | None = None
    #: Detected corners in rectified pixels, row-major, rounded to 1/100 px.
    corners: list[list[float]] | None = None
    width: int | None = None
    height: int | None = None
    skip_reason: str | None = None


#: Every model this module adds to the contract.
SLATE_CALIBRATION_MODELS = (
    PreprocessSlateImage,
    PreprocessSlateImagesInput,
    SlateObservation,
    SlateCalibrationInput,
    LaserCalibrationResult,
    CheckerboardTarget,
    CheckerboardCalibrationImage,
    PerformCheckerboardCalibrationInput,
    CheckerboardObservation,
    LatticeImage,
    VerifyCheckerboardLatticeInput,
    CheckerboardLatticeRender,
)
