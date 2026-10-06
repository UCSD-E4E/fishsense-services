"""Automatic results: the contract between the orchestrator and the processor.

New in v2 (no v1 counterpart). Fish lengths with no human label, by the chain
cscw-fishsense2027@96a8da07 validated (PAPER.md §6; e2e_measurement/run_e2e.py
runs it in research code):

1. the production laser detector's dot (`laser.LASER_PREDICTOR_VERSION`'s
   behaviour: the raw frame, rectified output, the expected-laser region);
2. a SAM 3.1 fish mask in the head/tail stage's crop around that dot, kept
   only if the dot is on it and SAM scores it at least `AUTOMATIC_SAM_SCORE_GATE`
   (§6.3: SAM's own confidence is the best mask gate);
3. the head/tail stage's geometric keypointer on the mask;
4. a label-free size-constancy calibration per dive (§4.3), from frames of a
   rigid object (the slate) with the dot on it;
5. the length at the dot's depth, as stage 14 measures.

BioCLIP's zero-shot species rides on each kept mask through the species
stage's own contract (`species_prediction`), cropped by the automatic mask's
box; the head/tail id it echoes is the automatic one.

**A separate track**: the processor's answers are stored as automatic rows,
never as labels or human-path predictions (decided 2026-10-06).

**Version the behaviour** (head/tail's rule). Bump
`AUTOMATIC_HEADTAIL_PREDICTOR_VERSION` when a frame's dot or head/tail would
differ for the same raw frame (detector, SAM checkpoint, gate, crop, prompt,
keypointer); `AUTOMATIC_CALIBRATION_VERSION` when the fit would; and
`AUTOMATIC_MEASUREMENT_VERSION` when a length would. History: 1 (2026-10-06)
the chain as cscw ran it, with the slate-presence detector stubbed (no frame
is a slate). **Bump the head/tail version when the detector lands**, so every
frame is re-classified as fish or slate (a slate frame keeps only its dot).

Not in ``MODELS``: integration publishes schemas once for every slice.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator

from fishsense_services_contracts._boxes import check_box
from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "AUTOMATIC_CALIBRATION_VERSION",
    "AUTOMATIC_HEADTAIL_PREDICTOR_VERSION",
    "AUTOMATIC_HEADTAIL_STATUSES",
    "AUTOMATIC_MEASUREMENT_ALGORITHM",
    "AUTOMATIC_MEASUREMENT_VERSION",
    "AUTOMATIC_SAM_SCORE_GATE",
    "AutomaticCalibrationFrame",
    "AutomaticCalibrationResult",
    "AutomaticFrame",
    "AutomaticFrameResult",
    "AutomaticLength",
    "AutomaticMeasureCapture",
    "FitAutomaticCalibrationInput",
    "MeasureAutomaticInput",
    "MeasureAutomaticResult",
    "PredictAutomaticFrameInput",
    "PredictAutomaticFramesInput",
]

#: See the module docstring.
AUTOMATIC_HEADTAIL_PREDICTOR_VERSION = 1
AUTOMATIC_CALIBRATION_VERSION = "1"
AUTOMATIC_MEASUREMENT_VERSION = "1"

#: Stage 14's single-depth fronto-parallel projection (processor
#: `measurement.activities.ALGORITHM`), with the depth gated to > 0.
AUTOMATIC_MEASUREMENT_ALGORITHM = "laser_depth_fronto_parallel"

#: The SAM 3.1 score a kept mask needs (paper §6.3: 0.5 covers 72 % of the
#: frames humans measured and gives 16 % of those they rejected a length;
#: lowering it trades one for the other almost one for one).
AUTOMATIC_SAM_SCORE_GATE = 0.5

#: What an automatic head/tail row may record. Every abstention is a row, so
#: the cohort, which selects on a row's absence, moves on.
AUTOMATIC_HEADTAIL_STATUSES = (
    "predicted",
    "no_laser_dot",
    "no_detections",
    "laser_off_all_fish",
    "headtail_failed",
    "decode_failed",
    "raw_unavailable",
    "slate_frame",
)


class AutomaticFrame(BaseModel):
    """One canonical frame: its staged raw, and where its rectified JPEG is
    (or goes: `write_jpeg`), which the species and calibration steps read."""

    capture_id: UUID
    raw: ObjectRef
    jpeg: ObjectRef
    #: The JPEG is not in Garage yet: write the rendering there.
    write_jpeg: bool = False
    #: The slate-presence detector's score (None: no detector). A slate frame
    #: keeps its dot for the calibration and is never measured as a fish.
    slate_probability: Optional[float] = None
    is_slate: bool = False


class PredictAutomaticFramesInput(BaseModel):
    """The GPU workflow's input (orchestrator -> processor)."""

    tenant_id: UUID
    dive_id: UUID
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    #: The expected-laser region (`laser_region.LASER_REGION_POLYGON`).
    laser_region: Optional[List[List[float]]] = None
    frames: List[AutomaticFrame]


class PredictAutomaticFrameInput(BaseModel):
    """What one `predict_automatic_frame` activity runs on."""

    frame: AutomaticFrame
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    laser_region: Optional[List[List[float]]] = None


class AutomaticFrameResult(BaseModel):
    """One frame's dot and head/tail, or which abstention. Rectified-frame
    pixels throughout."""

    capture_id: UUID
    status: str
    predictor_version: int
    laser_x: Optional[float] = None
    laser_y: Optional[float] = None
    laser_confidence: Optional[float] = None
    laser_predictor_version: Optional[int] = None
    laser_checkpoint: Optional[str] = None
    head_x: Optional[float] = None
    head_y: Optional[float] = None
    tail_x: Optional[float] = None
    tail_y: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    mask_area_px: Optional[int] = None
    silhouette_ratio: Optional[float] = None
    crop_x: Optional[int] = None
    crop_y: Optional[int] = None
    mask_bbox: Optional[List[int]] = None
    sam_score: Optional[float] = None
    slate_probability: Optional[float] = None
    checkpoint: Optional[str] = None
    core_version: Optional[str] = None

    @field_validator("mask_bbox")
    @classmethod
    def _box(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        return check_box(value)


class AutomaticCalibrationFrame(BaseModel):
    """A slate frame with its automatic dot, and its rectified JPEG."""

    capture_id: UUID
    jpeg: ObjectRef
    x: float
    y: float


class FitAutomaticCalibrationInput(BaseModel):
    tenant_id: UUID
    dive_id: UUID
    camera_matrix: List[List[float]]
    frames: List[AutomaticCalibrationFrame]
    #: Every automatic dot of the dive, for the dive line.
    line_dots: List[List[float]] = []


class AutomaticCalibrationResult(BaseModel):
    """The dive's label-free fit, or why not, with what it saw."""

    dive_id: UUID
    outcome: str
    refusal_reason: Optional[str] = None
    algorithm_version: str
    laser_position: Optional[List[float]] = None
    laser_axis: Optional[List[float]] = None
    vanishing_px: Optional[float] = None
    line_direction: Optional[List[float]] = None
    line_offset_px: Optional[float] = None
    o_mag_m: Optional[float] = None
    frames_used: int = 0
    candidate_count: int = 0
    pair_count: int = 0
    size_ratio: Optional[float] = None
    se_px: Optional[float] = None
    pair_residual_sd: Optional[float] = None
    #: The frames the fit used.
    capture_ids: List[UUID] = []
    core_version: Optional[str] = None


class AutomaticMeasureCapture(BaseModel):
    capture_id: UUID
    automatic_head_tail_prediction_id: UUID
    laser_x: float
    laser_y: float
    head_x: float
    head_y: float
    tail_x: float
    tail_y: float


class MeasureAutomaticInput(BaseModel):
    tenant_id: UUID
    dive_id: UUID
    camera_matrix: List[List[float]]
    laser_position: List[float]
    laser_axis: List[float]
    captures: List[AutomaticMeasureCapture]


class AutomaticLength(BaseModel):
    capture_id: UUID
    automatic_head_tail_prediction_id: UUID
    length_m: Optional[float] = None
    depth_m: Optional[float] = None
    #: Why no length: `non_positive_depth`, `non_finite_length`, `zero_length`.
    refusal: Optional[str] = None


class MeasureAutomaticResult(BaseModel):
    dive_id: UUID
    algorithm: str
    algorithm_version: str
    core_version: str
    lengths: List[AutomaticLength]
