"""The shadow size-constancy laser calibration's payloads (v13).

docs/shadow-size-constancy.md: SAM 3.1 segments each slate frame's render, the
size-constancy fit runs on the light role -- a port of
cscw-fishsense2027@8312780a ``analysis/codesign/size_constancy_sam.py`` and its
committed gated variant -- and the results are stored apart, feeding nothing.

* `SegmentSlateFramesInput` / `SlateSegment`: the GPU step. Each frame's
  render (presigned GET) is downscaled to `SHADOW_RENDER_LONG_SIDE` px on its
  long side, as the port's render cache was, and segmented with each prompt of
  `SHADOW_SAM_PROMPTS` in turn at `SHADOW_SAM_THRESHOLD`; for each dot asked
  about, the highest-scoring mask containing it is kept, and its size is the
  square root of its area (in render px; `render_scale` converts).
* `FitShadowCalibrationInput` / `ShadowFitResult`: the light step, per mission:
  both dot sources, both variants.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fishsense_services_contracts.renamed import renamed_from

__all__ = [
    "DotSegment",
    "FitShadowCalibrationInput",
    "SHADOW_ALGORITHM_VERSION",
    "SHADOW_DOT_SOURCES",
    "SHADOW_RENDER_LONG_SIDE",
    "SHADOW_SAM_PROMPTS",
    "SHADOW_SAM_THRESHOLD",
    "SegmentSlateFrame",
    "SegmentSlateFramesInput",
    "ShadowCalibrationOut",
    "ShadowFitFrame",
    "ShadowFitResult",
    "ShadowFrameOut",
    "SlateSegment",
]

#: The stage's algorithm version: the port and the §13 committed values.
SHADOW_ALGORITHM_VERSION = "1"
#: The port's prompts, tried in order (sam_sizes.py: "white board" won 253 of
#: the pool's 258 frames; "slate" never put a mask under the dot).
SHADOW_SAM_PROMPTS = ("slate", "white board")
#: The port's SAM confidence threshold (sam_sizes.py: 0.3; production's
#: shared processor keeps its own 0.5).
SHADOW_SAM_THRESHOLD = 0.3
#: The port's render: rectified, downscaled to this long side.
SHADOW_RENDER_LONG_SIDE = 1600
SHADOW_DOT_SOURCES = ("labels", "detector")


class SegmentSlateFrame(BaseModel):
    """One slate frame: its render, and the dots (full-res rectified px) to
    pick a mask under -- the label dot and the detector dot, either may be
    absent."""

    capture_id: UUID
    render_url: str
    dots: List[List[float]]


class SegmentSlateFramesInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    frames: List[SegmentSlateFrame]


class DotSegment(BaseModel):
    """The mask kept under one dot: None fields when no mask held it."""

    prompt: Optional[str] = None
    score: Optional[float] = None
    #: In render px (at `SHADOW_RENDER_LONG_SIDE`).
    mask_area_px: Optional[float] = None
    #: sqrt(mask_area_px).
    size: Optional[float] = None


class SlateSegment(BaseModel):
    """A frame's segmentation: one `DotSegment` per dot asked about, in
    order; ``render_unavailable`` / ``decode_failed`` with none."""

    capture_id: UUID
    status: str
    #: Full-res px per render px.
    render_scale: Optional[float] = None
    sam_model: Optional[str] = None
    dots: List[DotSegment] = []


class ShadowFitFrame(BaseModel):
    """One slate frame as the fit sees it, for one dot source."""

    capture_id: UUID
    frame_source: str
    slate_probability: Optional[float] = None
    dot_x: Optional[float] = None
    dot_y: Optional[float] = None
    dot_label_id: Optional[UUID] = None
    dot_label_source: Optional[str] = None
    dot_prediction_id: Optional[UUID] = None
    dot_confidence: Optional[float] = None
    sam_prompt: Optional[str] = None
    sam_score: Optional[float] = None
    mask_area_px: Optional[float] = None
    size: Optional[float] = None
    #: ``decode_failed`` when SAM could not read the frame's render: the
    #: frame is left out under that reason, not counted as no mask.
    segment_status: Optional[str] = None
    capture_number: Optional[int] = None
    captured_at: Optional[datetime] = None


class FitShadowCalibrationInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    #: Seeds the bootstrap, as the port's session number did.
    mission_number: int
    camera_calibration_id: Optional[UUID] = None
    camera_matrix: List[List[float]]
    #: ``(a, b, c)`` of the mission's laser line, or None.
    mission_line: Optional[List[float]] = None
    reference_laser_calibration_id: Optional[UUID] = None
    reference_borrowed: Optional[bool] = None
    reference_position: Optional[List[float]] = None
    reference_axis: Optional[List[float]] = None
    #: Per dot source (`SHADOW_DOT_SOURCES`): its frames.
    frames: Dict[str, List[ShadowFitFrame]]
    #: The detector line's population: every automatic dot ``[x, y, conf]``.
    mission_detector_dots: List[List[float]] = []
    #: Full-res px per render px, for the slate's size in meters.
    render_scale: float = 1.0
    sam_model: str


class ShadowFrameOut(BaseModel):
    capture_id: UUID
    frame_source: str
    used: bool
    rejected_reason: Optional[str] = None
    slate_probability: Optional[float] = None
    dot_x: Optional[float] = None
    dot_y: Optional[float] = None
    dot_label_id: Optional[UUID] = None
    dot_label_source: Optional[str] = None
    dot_prediction_id: Optional[UUID] = None
    dot_confidence: Optional[float] = None
    line_distance_px: Optional[float] = None
    sam_prompt: Optional[str] = None
    sam_score: Optional[float] = None
    mask_area_px: Optional[float] = None
    size: Optional[float] = None
    t_px: Optional[float] = None
    range_m: Optional[float] = None
    capture_number: Optional[int] = None
    captured_at: Optional[datetime] = None


class ShadowCalibrationOut(BaseModel):
    """One stored result: a dot source x variant (``port`` | ``gated``)."""

    dot_source: str
    variant: str
    outcome: str
    o_mag_m: float
    frames_used: int
    sam_model: str
    sam_prompts: List[str]
    sam_threshold: float
    render_long_side: int
    algorithm_version: str
    code_version: str
    refusal_reason: Optional[str] = None
    refusals: List[str] = []
    laser_position: Optional[List[float]] = None
    laser_axis: Optional[List[float]] = None
    vanishing_px: Optional[float] = None
    line_direction: Optional[List[float]] = None
    line_offset_px: Optional[float] = None
    line_source: Optional[str] = None
    camera_calibration_id: Optional[UUID] = None
    reference_laser_calibration_id: Optional[UUID] = None
    reference_borrowed: Optional[bool] = None
    angle_diff_deg: Optional[float] = None
    vp_offset_px: Optional[float] = None
    vp_offset_deg_fx: Optional[float] = None
    vp_offset_deg_f2850: Optional[float] = None
    fit_residual: Optional[float] = None
    se_px: Optional[float] = None
    se_deg: Optional[float] = None
    frames_rejected: Dict[str, int] = {}
    size_spread: Optional[float] = None
    range_min_m: Optional[float] = None
    range_max_m: Optional[float] = None
    lever_m: Optional[float] = None
    lever_m_production_offset: Optional[float] = None
    size_cv_pct: Optional[float] = None
    size_maxdev_pct: Optional[float] = None
    sam_score_min: Optional[float] = None
    sam_score_median: Optional[float] = None
    no_mask_share: Optional[float] = None
    gate: Dict[str, object] = {}
    frames: List[ShadowFrameOut] = []


class ShadowFitResult(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    calibrations: List[ShadowCalibrationOut]
