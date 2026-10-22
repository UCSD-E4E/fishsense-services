"""The mask backfill's payloads (v14): an existing prediction's SAM mask.

Masks have been stored since contract 12 (2026-10-21, `masks`); predictions
made before then kept theirs only as numbers (area, crop, box, head/tail). The
backfill re-runs the kernel that made each prediction -- the automatic
track's `automatic_frame_result` on its render, or the head/tail stage's
`predict_from_jpeg` on its stage-5.1 JPEG -- with the prediction's own dot,
and keeps the mask only when it `reproduces` the prediction's: the same crop
and the mask area within `MASK_AREA_TOLERANCE`. A mask that does not is not
that prediction's mask, and is never stored as it. Nothing here writes a
prediction.

**Judged by the mask, not the head/tail** (owner, 2026-10-22). On the trial
(missions 172 and 498, 142 predictions) the crop matched every time and 24 of
the 27 that missed a 1 px head/tail tolerance had the same mask to within
0.1% of its area -- SAM in bfloat16 on another GPU moves a few boundary
pixels -- while the keypointer moved head or tail 1-284 px. The regenerated
head/tail is recorded with every regeneration, for that repeatability.
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
    "MASK_SOURCES",
    "MaskExpectation",
    "reproduces",
]

#: Which predictions: the automatic track's, or the human-path head/tail's.
MASK_SOURCES = ("automatic", "headtail")
#: The most a regenerated mask's area may differ from the recorded one,
#: relative: SAM in bfloat16 on another GPU need not repeat bit for bit.
MASK_AREA_TOLERANCE = 0.005
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
    """Whether `got` is the mask `expected` recorded: its crop, and its area
    within the tolerance. Head and tail are not the mask's: they are the
    keypointer's reading of it, and are recorded, not judged."""
    if (got.crop_x, got.crop_y) != (expected.crop_x, expected.crop_y):
        return False
    return abs(got.mask_area_px - expected.mask_area_px) <= MASK_AREA_TOLERANCE * max(
        expected.mask_area_px, 1
    )


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
