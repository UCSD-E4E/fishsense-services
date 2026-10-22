"""The mask backfill's payloads (v14): an existing prediction's SAM mask.

Masks have been stored since contract 12 (2026-10-21, `masks`); predictions
made before then kept theirs only as numbers (area, crop, box, head/tail). The
backfill re-runs the kernel that made each prediction -- the automatic
track's `automatic_frame_result` on its render, or the head/tail stage's
`predict_from_jpeg` on its stage-5.1 JPEG -- with the prediction's own dot,
and keeps the mask only when it `reproduces` what the prediction recorded:
the same crop, the mask area within `MASK_AREA_TOLERANCE`, head and tail
within `MASK_POINT_TOLERANCE_PX`. A mask that does not is not that
prediction's mask, and is never stored as it. Nothing here writes a
prediction.
"""

from __future__ import annotations

from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, model_validator

from fishsense_services_contracts.masks import MaskUpload, StoredMask
from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "BACKFILL_OUTCOMES",
    "BackfilledMask",
    "BackfillMaskFrame",
    "BackfillMasksInput",
    "MASK_AREA_TOLERANCE",
    "MASK_POINT_TOLERANCE_PX",
    "MASK_SOURCES",
    "MaskExpectation",
    "reproduces",
]

#: Which predictions: the automatic track's, or the human-path head/tail's.
MASK_SOURCES = ("automatic", "headtail")
#: The most a regenerated mask's area may differ from the recorded one,
#: relative: SAM in bfloat16 on another GPU need not repeat bit for bit.
MASK_AREA_TOLERANCE = 0.005
#: The most a regenerated head or tail may sit from the recorded one, px.
MASK_POINT_TOLERANCE_PX = 1.0
BACKFILL_OUTCOMES = (
    "reproduced",  # stored
    "mismatch",  # SAM kept a different mask: not this prediction's
    "no_mask",  # SAM kept none under the dot now
    "image_unavailable",  # the render or JPEG is gone
    "decode_failed",
)


class MaskExpectation(BaseModel):
    """What a prediction recorded of its mask, or what a regeneration got."""

    mask_area_px: int
    crop_x: int
    crop_y: int
    head_x: Optional[float] = None
    head_y: Optional[float] = None
    tail_x: Optional[float] = None
    tail_y: Optional[float] = None


def reproduces(expected: MaskExpectation, got: MaskExpectation) -> bool:
    """Whether `got` is the mask `expected` recorded."""
    if (got.crop_x, got.crop_y) != (expected.crop_x, expected.crop_y):
        return False
    if abs(got.mask_area_px - expected.mask_area_px) > MASK_AREA_TOLERANCE * max(
        expected.mask_area_px, 1
    ):
        return False
    for a, b in (
        (expected.head_x, got.head_x), (expected.head_y, got.head_y),
        (expected.tail_x, got.tail_x), (expected.tail_y, got.tail_y),
    ):  # fmt: skip
        if (a is None) != (b is None):
            return False
        if a is not None and abs(a - b) > MASK_POINT_TOLERANCE_PX:
            return False
    return True


class BackfillMaskFrame(BaseModel):
    """One prediction to regenerate: its image (a render by presigned GET for
    the automatic track, the stage-5.1 JPEG for the head/tail stage), the
    dot(s) it was made from, what it recorded, and where to PUT the mask."""

    prediction_id: UUID
    capture_id: UUID
    dots: List[List[float]]
    #: The head/tail stage's dots' labels, parallel to `dots`.
    laser_label_ids: Optional[List[UUID]] = None
    render_url: Optional[str] = None
    jpeg: Optional[ObjectRef] = None
    expect: MaskExpectation
    mask: Optional[MaskUpload] = None

    @model_validator(mode="after")
    def _one_image(self) -> "BackfillMaskFrame":
        if (self.render_url is None) == (self.jpeg is None):
            raise ValueError("a frame names a render_url or a jpeg, not both")
        return self


class BackfillMasksInput(BaseModel):
    tenant_id: UUID
    source: Literal["automatic", "headtail"]
    frames: List[BackfillMaskFrame]


class BackfilledMask(BaseModel):
    prediction_id: UUID
    capture_id: UUID
    outcome: Literal[
        "reproduced", "mismatch", "no_mask", "image_unavailable", "decode_failed"
    ]
    #: What the regeneration got, for the report (None with no mask).
    got: Optional[MaskExpectation] = None
    #: The stored mask: only when reproduced.
    mask: Optional[StoredMask] = None

    @model_validator(mode="after")
    def _named_only_when_reproduced(self) -> "BackfilledMask":
        if self.mask is not None and self.outcome != "reproduced":
            raise ValueError(f"a {self.outcome} regeneration names no mask")
        return self
