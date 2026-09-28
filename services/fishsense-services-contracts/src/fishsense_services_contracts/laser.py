"""The laser slice's contract: what the orchestrator and processor exchange for
stage 0.1 (preprocess), laser prediction, the auto-accept gate, laser-label
validation and its remediation.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
(preprocess_contracts.py's laser DTOs, laser_predictor.py,
auto_accept_timeouts.py, laser_remediation.py). v1's constants are kept; the
payloads change shape for v2's rules:

* **the processor never reads the database** (PLAN.md §9.11). In v1 the gate,
  the validator and remediation ran on the data-worker and called the API
  themselves; in v2 the orchestrator reads the rows, hands them over here, and
  writes back what the processor decided. So those payloads are rows in,
  verdicts out;
* **it is handed ``ObjectRef``s, never checksums**: only the orchestrator
  issues keys;
* rows are identified by uuid for the write back, and carry their ``number``
  (v1's id for a migrated row) wherever v1 ordered or hashed by id: the
  validator's (image_id, id) order, the audit sample's (dive, image) key, and
  the remediation report.

Not yet in ``MODELS``: the integration publishes the schema once for every
slice (docs/port-plan.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from enum import Enum
from typing import Annotated, Iterable, Literal, Sequence, Tuple
from uuid import UUID

from annotated_types import Len
from pydantic import BaseModel, Field, model_validator

from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "GATE_ACTIVITY_TIMEOUT",
    "GATE_CHILD_EXECUTION_TIMEOUT",
    "GATE_EXECUTION_TIMEOUT",
    "GATE_QUEUE_WAIT_TIMEOUT",
    "GATE_VERDICTS",
    "LASER_PREDICTOR_VERSION",
    "NOISE_ESTIMATOR",
    "DivePlan",
    "EvaluateLaserAutoAcceptInput",
    "GatePrediction",
    "LaserAutoAcceptResult",
    "LaserAutoAcceptSummary",
    "LaserFrameVerdict",
    "LaserLabelRow",
    "LaserLineFit",
    "LaserPredictImage",
    "LaserPredictionResult",
    "LaserPreprocessImage",
    "LaserSupersede",
    "LaserValidationResult",
    "PlanLaserRemediationInput",
    "PredictLaserImagesInput",
    "PreprocessLaserImagesInput",
    "ReflectionReport",
    "RemediateLaserSupersedesInput",
    "RemediationDiveRequest",
    "SupersedeReason",
    "ValidateLaserLabelsInput",
    "laser_model_version_tag",
    "revival_digest",
]

# --- the stage version (v1's laser_predictor.py) ----------------------------

#: Bumped by hand when the laser-detector stage's output would change for an
#: unchanged image. It versions the behaviour, not the checkpoint: the region
#: polygon, the colour rule, the gate and core's `predict()` defaults are all
#: inputs to it. History (v1's):
#:   1 -- the original stage: no output gate, no colour, "Red Laser" always.
#:   2 -- 2026-08-28: predictions outside `LASER_REGION_POLYGON` are rejected,
#:        and the laser's colour is read off the dot.
LASER_PREDICTOR_VERSION: int = 2


def laser_model_version_tag(version: int = LASER_PREDICTOR_VERSION) -> str:
    """The `model_version` stamped on a Label Studio pre-annotation: both what
    a labeler sees attributed to the model and what makes re-attaching
    predictions idempotent (the backfill skips a task already carrying it)."""
    return f"laser-detector-v{version}"


# --- the gate's budget (v1's auto_accept_timeouts.py) ------------------------
#
# Queue wait and execution are bounded SEPARATELY, and that split is the point:
# v1 shipped the gate with one conflated 15-minute bound, and on 2026-09-04 two
# of the drain's first three firings expired on ScheduleToStart behind rawpy
# decodes, for a fit that takes under a second.

#: How long the gate activity may wait for a slot on the light queue.
GATE_QUEUE_WAIT_TIMEOUT = timedelta(minutes=20)

#: How long it may run once it has one. v2: 5 minutes, not v1's 10 -- v1's
#: covered the data-worker's own fetch of the dive's predictions, and the v2
#: processor is handed them. The orchestrator's read and write steps took the
#: difference, inside the parent's unchanged 1 h run timeout.
GATE_EXECUTION_TIMEOUT = timedelta(minutes=5)

#: `schedule_to_close`: the sum, never less.
GATE_ACTIVITY_TIMEOUT = GATE_QUEUE_WAIT_TIMEOUT + GATE_EXECUTION_TIMEOUT

#: What both parents pass as the child's `execution_timeout`. Must exceed the
#: activity's budget, so the activity's timeout is the one that fires and names
#: the bound that was hit.
GATE_CHILD_EXECUTION_TIMEOUT = timedelta(minutes=30)


# --- stage 0.1: preprocess --------------------------------------------------

Matrix3 = list[list[float]]
Region = list[list[int]]


class LaserPreprocessImage(BaseModel):
    """One canonical capture to redraw: its staged raw frame, and where its
    JPEG goes (over v1's JPEG for a migrated frame, else the tenant's key)."""

    capture_id: UUID
    raw: ObjectRef
    jpeg: ObjectRef


class PreprocessLaserImagesInput(BaseModel):
    """v1's `PreprocessLaserImagesInput`. `laser_region` is what is drawn;
    `bbox` is its bounding box, kept for v1's rolling-deploy reason (a
    processor predating the polygon draws the rectangle)."""

    dive_id: UUID
    images: list[LaserPreprocessImage]
    camera_matrix: Matrix3
    distortion_coefficients: list[float]
    bbox: Annotated[list[int], Len(4, 4)]
    laser_region: Region | None = None


# --- laser prediction (GPU) -------------------------------------------------


class LaserPredictImage(BaseModel):
    capture_id: UUID
    raw: ObjectRef


class PredictLaserImagesInput(BaseModel):
    """v1's `PredictLaserImagesInput`. `wavelength` None is the detector's
    unknown-wavelength channel (v1 always sent None); a prediction outside
    `laser_region` is not believed, and None disables that gate."""

    dive_id: UUID
    images: list[LaserPredictImage]
    camera_matrix: Matrix3
    distortion_coefficients: list[float]
    wavelength: Literal["red", "green"] | None = None
    laser_region: Region | None = None


class LaserPredictionResult(BaseModel):
    """One frame's prediction, in rectified pixels (v1's). x/y are both set or
    both None (no dot, or rejected out of region)."""

    capture_id: UUID
    x: float | None = None
    y: float | None = None
    confidence: float
    width: int | None = None
    height: int | None = None
    color: Literal["red", "green"] | None = None
    color_margin: float | None = None
    rejected_out_of_region: bool = False
    predictor_version: int
    checkpoint: str | None = None
    core_version: str | None = None

    @model_validator(mode="after")
    def _dot_or_none(self) -> "LaserPredictionResult":
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y are both set or both None")
        return self


# --- the auto-accept gate (light) -------------------------------------------

GATE_VERDICTS = (
    "auto_accepted",
    "off_line",
    "along_line_outlier",
    "no_prediction",
    "audit_sample",
    "dive_ineligible",
)
GateVerdict = Literal[
    "auto_accepted",
    "off_line",
    "along_line_outlier",
    "no_prediction",
    "audit_sample",
    "dive_ineligible",
]


class GatePrediction(BaseModel):
    """One current prediction of the dive, as the gate reads it."""

    prediction_id: UUID
    #: The capture's number: v1's image id for a migrated frame, the audit
    #: sample's key.
    capture_number: int
    x: float | None = None
    y: float | None = None
    predictor_version: int | None = None


class EvaluateLaserAutoAcceptInput(BaseModel):
    dive_id: UUID
    #: v1's dive id for a migrated dive: the audit sample is keyed on
    #: (dive, image), so a migrated dive samples the frames v1 did.
    dive_number: int
    predictions: list[GatePrediction]


class LaserFrameVerdict(BaseModel):
    """One prediction's verdict and margins (v1's gate columns)."""

    prediction_id: UUID
    auto_accept: bool
    gate_verdict: GateVerdict
    line_offset_px: float | None = None
    line_position_z: float | None = None


class LaserAutoAcceptSummary(BaseModel):
    """v1's per-dive summary: the monitoring signal for the stage. It counts
    the flag (`auto_accepted`), not the verdict histogram -- with the gate
    disabled the two disagree on purpose."""

    dive_id: UUID
    enabled: bool = True
    eligible: bool = False
    reason: str | None = None
    n_points: int = 0
    inlier_count: int = 0
    inlier_fraction: float = 0.0
    line_confidence: float = 0.0
    auto_accepted: int = 0
    verdicts: dict[str, int] = Field(default_factory=dict)
    #: Verdicts the orchestrator recorded (only changed ones are written).
    written: int = 0


class LaserAutoAcceptResult(BaseModel):
    summary: LaserAutoAcceptSummary
    frames: list[LaserFrameVerdict]


# --- laser-label validation and remediation (light) -------------------------

#: fishsense-core >= 4.1.0's noise scale: MAD over *signed* residuals, about
#: 1.0 sigma (v1's vendored fit folded them, about 0.59). Migration 0017.
NOISE_ESTIMATOR = "signed_residual_mad"


class LaserLabelRow(BaseModel):
    """A laser label as the judgement reads it. Superseded rows included: the
    validator judges the full population (fishsense-core #88, lite #927)."""

    label_id: UUID
    #: v1's label id for a migrated row: the tiebreak of v1's (image_id, id).
    number: int
    #: v1's image id for a migrated capture: the first key of that order.
    capture_number: int
    x: float | None = None
    y: float | None = None
    superseded: bool
    completed: bool


class ValidateLaserLabelsInput(BaseModel):
    dive_id: UUID
    labels: list[LaserLabelRow]
    #: Frames carrying a completed, live slate label: judged at the coarse
    #: calibration bound, not 3 sigma.
    calibration_capture_numbers: list[int] = Field(default_factory=list)


class SupersedeReason(str, Enum):
    """The reasons the validator writes (laser_labels.superseded_reason)."""

    VALIDATOR_3SIGMA = "validator_3sigma"
    VALIDATOR_COARSE_CALIBRATION = "validator_coarse_calibration"


class LaserSupersede(BaseModel):
    label_id: UUID
    reason: SupersedeReason


class LaserLineFit(BaseModel):
    """The dive's within-dive line (dive_laser_lines). Never a prior for
    another dive."""

    a: float
    b: float
    c: float
    n_points: int
    inlier_count: int
    inlier_fraction: float
    residual_std: float
    label_noise_mad: float
    line_confidence: float
    noise_estimator: Literal["signed_residual_mad"] = NOISE_ESTIMATOR


class ReflectionReport(BaseModel):
    n_primary: int
    n_secondary: int
    separation_px: float
    angle_deg: float


class LaserValidationResult(BaseModel):
    """What one judgement of the dive decided, for the orchestrator to write:
    the line (whenever one was fitted, as v1 wrote it) and the rows to
    supersede -- flagged *and still live*, never anything else."""

    dive_id: UUID
    status: str
    positives: int
    flagged: int = 0
    line: LaserLineFit | None = None
    supersede: list[LaserSupersede] = Field(default_factory=list)
    reflection: ReflectionReport | None = None


class PlanLaserRemediationInput(BaseModel):
    """One dive's plan request, with its rows (v1's RemediationDiveRequest,
    read by the data-worker itself)."""

    dive_id: int
    labels: list[LaserLabelRow]
    calibration_capture_numbers: list[int] = Field(default_factory=list)
    excluded_label_ids: list[int] = Field(default_factory=list)
    dive_excluded: bool = False


class DivePlan(BaseModel):
    """One dive's row of the remediation report (v1's), by number."""

    dive_id: int
    status: str
    positives: int
    superseded_now: int
    superseded_after: int
    revive_ids: list[int] = Field(default_factory=list)
    excluded_kept: list[int] = Field(default_factory=list)
    unjudged_superseded: int = 0
    reflection_suspect: dict | None = None
    revive_on_calibration_frames: list[int] = Field(default_factory=list)
    revive_on_images_with_another_live_label: list[int] = Field(default_factory=list)

    def to_dict(self) -> dict:
        """A JSON-ready report row."""
        return self.model_dump(mode="json")


class RemediateLaserSupersedesInput(BaseModel):
    """One remediation run, by dive and label number. Dry run unless `apply`
    AND a matching digest."""

    dive_ids: list[int]
    excluded_dive_ids: list[int] = Field(default_factory=list)
    excluded_label_ids: list[int] = Field(default_factory=list)
    apply: bool = False
    #: The `plan_sha256` of the reviewed dry-run report.
    expected_plan_sha256: str | None = None


class RemediationDiveRequest(BaseModel):
    """Plan (or apply) one dive. `revive_ids` is read by apply only."""

    dive_id: int
    excluded_label_ids: list[int] = Field(default_factory=list)
    dive_excluded: bool = False
    revive_ids: list[int] = Field(default_factory=list)


def revival_digest(rows: Iterable[Tuple[int, Sequence[int]]]) -> str:
    """sha256 over exactly the revivals `(dive, revive ids)`, order-free (v1's,
    byte for byte: numbers are v1's ids, so v1's digests still match)."""
    canonical = sorted(
        (int(dive_id), sorted(int(i) for i in ids)) for dive_id, ids in rows if ids
    )
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()
