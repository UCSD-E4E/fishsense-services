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
is a slate); 2 (2026-10-16) the detector wired (a slate frame keeps only its
dot), and a kept mask whose SAM score can't be named under the dot is
``laser_off_all_fish``, not an unscored prediction. No version-1 row was ever
written in production. 3 (2026-10-19) every frame's render -- rectified from
its raw, the image its dot and mask were found in -- is written under
`object_store.automatic_jpeg_folder`, and calibration and species read only
that; under 2
a migrated frame kept v1's head/tail JPEG, which they then read. A frame's dot
and head/tail are as under 2; the bump re-runs every frame to write its render.
4 (2026-10-22) a kept mask cut by an edge of the 1800x1350 window that is
not the photo's own is segmented again on the whole photo, and the frame
abstains if no mask there is under the dot: a fish longer than the window came
back cut straight across, its head and tail read off the cut (lab mission 383:
a 686 mm Nassau Grouper measured 554 mm; 1,346 of 38,923 masks were cut, and
on the 74 with a human length the error ran -23 % to +25 %, p10-p90). Every
other frame is as under 3, so the API counts a version-3 row of an uncut mask
as done (`automatic_results_store.head_tail_current`) and the drain re-runs
only the cut ones.

`AUTOMATIC_MEASUREMENT_VERSION` 2 (2026-10-23, fishsense-core #96): the dot
must pass `LaserPoint.is_measurable()` -- finite, in front of the camera, and
its camera ray at least 0.005 rad off the laser -- where version 1 asked only
for a positive depth. Near the laser's vanishing point the triangulation
returns a finite, positive, untrustworthy depth (a pixel of dot error moves it
more than 7 % there); version 2 refuses it as `low_parallax`. Every other
length is unchanged to float precision (20,000 random dots: core matched
version 1's geometry exactly), but the cohort selects on the version, so the
bump measures every automatic frame again (light queue, geometry only).

`AUTOMATIC_JPEG_RENDER_VERSION` names that render: the raw decoded and
rectified by fishsense-core with the camera's matrix and distortion, then
encoded (processor `headtail_preprocess.rectify_and_encode_jpeg`). It and the
camera calibration the raw was rectified with are the JPEG's key
(`object_store.automatic_jpeg_folder`) and are recorded on each frame's row,
so a re-render never overwrites an image older rows' pixels belong to, and the
frames to re-render are the rows at an older version or camera. Bump
it when the rendering would change a pixel, and bump
`AUTOMATIC_HEADTAIL_PREDICTOR_VERSION` with it: the dot and mask are found
against the render. History: 1 (2026-10-19) as above.

`AUTOMATIC_CALIBRATION_VERSION` 2 (2026-10-19): slate dots more than 10 px off
the dive line are not fitted (processor `automatic_calibration.activities`),
and a frame whose size disagrees with its place on the line is dropped
(`size_constancy.trim_frames`). Version 1 fitted both: a reflection's run of
off-line dots moved session 77's vanishing point by 1,600 px, and one
wrong-range dot moved mission 80's by 360 px.
``size-constancy-3`` (2026-10-25): the size-constancy fit is fishsense-core
4.3.2's (`fishsense_core.size_constancy`), which absorbed this repo's port of
cscw's. Named, not numbered, so a row cannot be read as calibration v3 (the
screen-consensus-check replay, `automatic_calibration.v3`), which no row
records (review of #354). Core sizes only the largest connected component
of the pairs the robust pass kept, drops a pair beyond 10x before solving,
and measures no scale from a pair whose inliers collapse to one spot; with no
dive line the direction follows the trim. On cscw's ten sessions: nine fit
as under 2 (to 1e-11 px), and session 114 keeps all 29 frames with its
collapsed pair (log scale -37) counted implausible, its error against the
stored calibration -0.0446 -> -0.0439 deg. Production's cohort was re-fitted
read-only before this shipped (#354). The bump re-fits every dive in the
cohort (`automatic_results_store._CALIBRATION_WORK` selects on
`algorithm_version`); each accepted fit is a new row, so the lengths that
used the dive's label-free calibration are measured again.

Not in ``MODELS``: integration publishes schemas once for every slice.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Annotated
from uuid import UUID

from pydantic import BaseModel, field_validator, ConfigDict, model_validator

from fishsense_services_contracts._boxes import check_box, check_one_image
from fishsense_services_contracts.masks import MaskUpload, StoredMask, check_mask
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.renamed import renamed_from
from fishsense_services_contracts.renders import RENDER_VERSION

__all__ = [
    "AUTOMATIC_CALIBRATION_VERSION",
    "AUTOMATIC_HEADTAIL_PREDICTOR_VERSION",
    "AUTOMATIC_HEADTAIL_STATUSES",
    "AUTOMATIC_JPEG_RENDER_VERSION",
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
    "SLATE_POSE_CALIBRATION_VERSION",
]

#: See the module docstring.
AUTOMATIC_HEADTAIL_PREDICTOR_VERSION = 4
#: The one render every step reads (`renders.RENDER_VERSION`).
AUTOMATIC_JPEG_RENDER_VERSION = RENDER_VERSION
AUTOMATIC_CALIBRATION_VERSION = "size-constancy-3"
#: The slate-pose fit (calibration v4; 0078, the owner 2026-10-26): each slate
#: frame posed from its printed template, the beam through the posed dots.
#: Behind `FISHSENSE_AUTOMATIC_RESULTS_SLATE_POSE_ENABLED`, off by default.
SLATE_POSE_CALIBRATION_VERSION = "slate-pose-4"
AUTOMATIC_MEASUREMENT_VERSION = "2"

#: Stage 14's single-depth fronto-parallel projection (processor
#: `measurement.activities.ALGORITHM`), behind fishsense-core's
#: `LaserPoint.is_measurable()`.
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
    # A kept mask whose SAM score can't be named under the dot (0044).
    "mask_unscored",
    # Left out of `AUTOMATIC_FRAME_MAX_FAILURES` runs (0044): terminal.
    "frame_failed",
)


class AutomaticFrame(BaseModel):
    """One canonical frame: its staged raw, and where its render of the raw
    goes (`jpeg`, under `jpeg_render_version`'s folder), which the species and
    calibration steps read."""

    capture_id: UUID
    raw: ObjectRef
    #: Contracts 7-8: where the frames stage wrote its own render. From
    #: contract 9 the render stage writes it, and this is None.
    jpeg: Optional[ObjectRef] = None
    #: Contract 7's: write the render to `jpeg` only if set. Read only for a
    #: payload with no `jpeg_render_version` (resolved before contract 8).
    write_jpeg: bool = False
    #: The render the dot and mask are found against (contract 8: written to
    #: `jpeg`; contract 9: read from `render_url`).
    jpeg_render_version: Optional[int] = None
    #: Contract 9: a presigned GET of the capture's render in its tenant's
    #: bucket (`renders.render_key`), which SAM segments.
    render_url: Optional[str] = None
    #: The slate-presence detector's score (None: no detector). A slate frame
    #: keeps its dot for the calibration and is never measured as a fish.
    slate_probability: Optional[float] = None
    is_slate: bool = False
    #: Where to PUT the frame's mask, should it keep one (v12, `masks`).
    mask: Optional[MaskUpload] = None


class PredictAutomaticFramesInput(BaseModel):
    """The GPU workflow's input (orchestrator -> processor)."""

    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    #: The camera calibration those intrinsics are: part of the render's key.
    camera_calibration_id: Optional[UUID] = None
    #: The expected-laser region (`laser_region.LASER_REGION_POLYGON`).
    laser_region: Optional[List[List[float]]] = None
    frames: List[AutomaticFrame]


class PredictAutomaticFrameInput(BaseModel):
    """What one `predict_automatic_frame` activity runs on."""

    frame: AutomaticFrame
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    laser_region: Optional[List[List[float]]] = None
    camera_calibration_id: Optional[UUID] = None


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
    #: The dot's colour, read off its pixels by the laser stage's kernel
    #: (`laser.LaserPrediction.color`), and how clearly (v22, 2026-10-25).
    #: None when the dot gave no reading, or from a v21 processor.
    laser_color: Optional[Literal["red", "green"]] = None
    laser_color_margin: Optional[float] = None
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
    #: The render of the raw the dot and mask were found in, written to the
    #: frame's `jpeg`, and the camera calibration it was rectified with; None
    #: when no render was written (the raw would not decode, or a contract-7
    #: payload).
    jpeg_render_version: Optional[int] = None
    jpeg_camera_calibration_id: Optional[UUID] = None
    #: The kept mask, as stored (v12, `masks`); never recorded on the row.
    mask: Optional[StoredMask] = None

    @model_validator(mode="after")
    def _mask_kept(self) -> "AutomaticFrameResult":
        check_mask(self.status, self.mask)
        return self

    @field_validator("mask_bbox")
    @classmethod
    def _box(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        return check_box(value)


class AutomaticCalibrationFrame(BaseModel):
    """A slate frame with its automatic dot, and its render: by presigned URL
    (`jpeg_url`, the capture's render in its tenant's bucket), or -- before
    renders -- a stage's JPEG by reference (`jpeg`)."""

    capture_id: UUID
    jpeg: Optional[ObjectRef] = None
    jpeg_url: Optional[str] = None
    x: float
    y: float

    @model_validator(mode="after")
    def _one_image(self) -> "AutomaticCalibrationFrame":
        check_one_image(self.jpeg, self.jpeg_url)
        return self


class FitAutomaticCalibrationInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    camera_matrix: List[List[float]]
    frames: List[AutomaticCalibrationFrame]
    #: Every automatic dot of the dive, for the dive line.
    line_dots: List[List[float]] = []


class AutomaticCalibrationResult(BaseModel):
    """The dive's label-free fit, or why not, with what it saw."""

    model_config = ConfigDict(validate_by_name=True)

    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
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
    model_config = ConfigDict(validate_by_name=True)
    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    camera_matrix: List[List[float]]
    laser_position: List[float]
    laser_axis: List[float]
    captures: List[AutomaticMeasureCapture]


class AutomaticLength(BaseModel):
    capture_id: UUID
    automatic_head_tail_prediction_id: UUID
    length_m: Optional[float] = None
    depth_m: Optional[float] = None
    #: Why no length: the dot's reason (`laser_depth.DotRefusal`:
    #: `non_finite_depth`, `non_positive_depth`, `low_parallax`; version 2,
    #: and before it a non-finite depth was stored as `non_positive_depth`),
    #: `non_finite_length`, `zero_length`, or `zoomed` (the CLI's withdrawal,
    #: 0070). The API's CHECK repeats them (migration 0071).
    refusal: Optional[str] = None


class MeasureAutomaticResult(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    algorithm: str
    algorithm_version: str
    core_version: str
    lengths: List[AutomaticLength]
