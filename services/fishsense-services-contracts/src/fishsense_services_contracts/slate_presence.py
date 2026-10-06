"""Slate presence: the contract. Is a dive slate anywhere in this frame?

**New in v2.** v1's slate predictor estimated a slate's *pose*, only ever saw
frames that held one, and was retired on 2026-08-03; this model answers
presence only, so slate frames can be found in dives nobody has labelled. It
is the lab's 2026-10-03_slate_detector@95a77d95 (src/slate_detector/train.py,
render.py): ImageNet EfficientNet-B0 with GeM pooling in place of average
pooling (the slate is about 1% of the frame), over the whole rectified frame
resized to 1024x768, never cropped, the frame and its horizontal flip
averaged. The constants live here because both sides read them: the processor
stamps the version, and the orchestrator's cohort selects on a mismatch with
it and queues frames at or above the threshold for labelling.

**Two readers.** A prediction tags a frame for the automatic chain (excluded
from fish measurement, a calibration candidate) and, in a dive with no slate
labels, queues it in the dive's slate Label Studio project, so slate
calibration needs no frame-hunting. A human still places every reference
point.

**Every canonical frame, publication-grade** (owner's decision,
2026-10-05): every canonical frame of every dive, at every priority and
labelled or not, is scored once per model version, and each result records
enough to reproduce it -- model name and version, weights sha256,
fishsense-core and processor versions, the render (`SlateRender`), the
probability itself and when -- so the paper can evaluate the model against
the human answers (`slate_presence_evaluation`).

**Version the behaviour, not the model** (head/tail's rule). Bump
`SLATE_DETECTOR_VERSION` by hand whenever the output would differ for an
unchanged raw frame: new weights, another decode or resize, another TTA.
History: 1 (2026-10-05) runs/final-q1/slate_efficientnet_b0.pt (sha256
b8d377ba...cf78; 6,828 frames, 1,405 slate, 240 dives). 5-fold CV grouped by
dive: ROC AUC 0.9994, precision 0.999 and recall 0.993 at 0.5.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "SLATE_CACHE_LONG_SIDE",
    "SLATE_DETECTOR_MODEL_NAME",
    "SLATE_DETECTOR_VERSION",
    "SLATE_INPUT_HEIGHT",
    "SLATE_INPUT_WIDTH",
    "SLATE_JPEG_QUALITY",
    "SLATE_PRESENCE_STATUSES",
    "SLATE_PRESENCE_THRESHOLD",
    "SLATE_TTA",
    "DetectSlateImage",
    "DetectSlateImageInput",
    "DetectSlateImagesInput",
    "SlatePresenceResult",
    "SlateRender",
    "is_slate",
]

#: Bumped by hand when the output would change for an unchanged raw frame (see
#: the module docstring for the history).
SLATE_DETECTOR_VERSION = 1

#: P(slate) at or above this is a slate frame: the operating point the CV
#: numbers were measured at.
SLATE_PRESENCE_THRESHOLD = 0.5

#: What a row may record: a probability, or a raw that would not decode (an
#: abstention is persisted too: the cohort selects on a row's absence).
SLATE_PRESENCE_STATUSES = ("predicted", "decode_failed")

#: The model's name in `model-weights` and in every row.
SLATE_DETECTOR_MODEL_NAME = "slate-detector"
#: The model's input: the whole frame, resized (never cropped), 4:3.
SLATE_INPUT_WIDTH = 1024
SLATE_INPUT_HEIGHT = 768
#: The source repo's frame cache the model was trained on.
SLATE_CACHE_LONG_SIDE = 1600
SLATE_JPEG_QUALITY = 95
SLATE_TTA = "hflip"


def is_slate(probability: float) -> bool:
    """Whether a probability is a slate frame at the operating point."""
    return probability >= SLATE_PRESENCE_THRESHOLD


def _camera_matrix(value: List[List[float]]) -> List[List[float]]:
    if len(value) != 3 or any(len(row) != 3 for row in value):
        raise ValueError("camera_matrix must be 3x3")
    return value


class DetectSlateImage(BaseModel):
    """One canonical frame: its staged raw, a ref the orchestrator issued."""

    capture_id: UUID
    raw: ObjectRef


class DetectSlateImagesInput(BaseModel):
    """The detect workflow's input (orchestrator -> processor): a dive's
    frames and the intrinsics that rectify them (its device's current pinhole
    calibration)."""

    tenant_id: UUID
    dive_id: UUID
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    images: List[DetectSlateImage]

    @field_validator("camera_matrix")
    @classmethod
    def _matrix(cls, value: List[List[float]]) -> List[List[float]]:
        return _camera_matrix(value)


class DetectSlateImageInput(BaseModel):
    """What one `detect_slate_presence` activity scores."""

    image: DetectSlateImage
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]

    @field_validator("camera_matrix")
    @classmethod
    def _matrix(cls, value: List[List[float]]) -> List[List[float]]:
        return _camera_matrix(value)


class SlateRender(BaseModel):
    """How the frame was turned into the model's input: enough to reproduce
    it from the raw. The defaults are the only render version 1 runs."""

    #: fishsense-core's `DecodeConfig` constructor (`production`), and every
    #: field of the config it built.
    decode_config: str
    decode_params: Dict[str, Any]
    rectified: bool = True
    #: The training cache's shrink and JPEG round trip.
    cache_long_side: int = SLATE_CACHE_LONG_SIDE
    jpeg_quality: int = SLATE_JPEG_QUALITY
    input_width: int = SLATE_INPUT_WIDTH
    input_height: int = SLATE_INPUT_HEIGHT
    #: Test-time augmentation: the frame and its horizontal flip, logits
    #: averaged.
    tta: str = SLATE_TTA


class SlatePresenceResult(BaseModel):
    """One frame's prediction (processor -> orchestrator). `probability` is
    P(slate), set exactly when the frame decoded. Publication-grade: it names
    the model and weights, the fishsense-core and processor that ran it, the
    render, and when."""

    capture_id: UUID
    status: str
    probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    model_name: str = SLATE_DETECTOR_MODEL_NAME
    model_version: int
    #: The verified weights' sha256: what the prediction ran.
    weights_sha256: str
    #: Installed package versions (the processor image's release); None only
    #: when the metadata cannot be read.
    core_version: Optional[str] = None
    processor_version: Optional[str] = None
    render: SlateRender
    predicted_at: AwareDatetime

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        if value not in SLATE_PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {SLATE_PRESENCE_STATUSES}")
        return value

    @field_validator("weights_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("weights_sha256 must be a sha256 hex digest")
        return value

    @model_validator(mode="after")
    def _scored_iff_predicted(self) -> "SlatePresenceResult":
        if (self.status == "predicted") != (self.probability is not None):
            raise ValueError("a probability is set exactly when status is predicted")
        return self
