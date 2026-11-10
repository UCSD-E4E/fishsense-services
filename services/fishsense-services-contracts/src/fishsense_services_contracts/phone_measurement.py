"""The phone measurement's payloads (v28): a phone capture measured on the
server the way FishSense Mobile measures it.

New in v2 (2026-11-09). The owner's rule (2026-10-21): the server measures a
phone capture that was never measured, replicating the app (its rust-bridge
`compute_length`, fishsense-core 4.1.0), not the OCEANS 2025 paper. The
sequence is a port for now; one copy both sides call is #475.

The processor reads the capture's stored files by presigned GET: the JPEG the
phone saved (``rgb_jpeg``) and its LiDAR depth (``depth_f32``, float32 meters,
rows ``depth_row_bytes`` apart as ARKit pads them), with the K the capture
recorded. The result is the server's, unvalidated (the owner, 2026-11-09):
it is never a ``measurements`` row.

A frame may carry a mask upload: a result that kept a mask (the fish was
segmented) PUTs it, as an 8-bit grayscale PNG at the frame (`masks`), under
the ``phone`` source.
"""

from __future__ import annotations

from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from fishsense_services_contracts.masks import MaskUpload, StoredMask

__all__ = [
    "PHONE_ALGORITHM",
    "PHONE_MASK_STATUSES",
    "PHONE_STATUSES",
    "PhoneFrame",
    "PhoneMeasured",
    "PhoneMeasureInput",
]

#: The sequence the server runs: the app's, at the commit it was ported from.
PHONE_ALGORITHM = "fishsense-mobile@cd120f4"

PHONE_STATUSES = (
    "measured",
    "no_fish",  # segmentation kept no fish
    "headtail_failed",  # no snout and fork from the mask
    "depth_failed",  # no depth at a point: the plane fit and its fallback failed
    "image_unavailable",  # a stored file is gone
    "decode_failed",  # the JPEG or the depth would not read
)
#: The statuses that segmented a fish, and so kept a mask to store.
PHONE_MASK_STATUSES = ("measured", "headtail_failed", "depth_failed")


class PhoneFrame(BaseModel):
    """One phone capture to measure: its files, its depth's geometry, its K
    (row-major, the frame's pixels), and where to PUT the mask."""

    capture_id: UUID
    rgb_url: str
    depth_url: str
    depth_width: int = Field(gt=0)
    depth_height: int = Field(gt=0)
    depth_row_bytes: int = Field(gt=0)
    intrinsics: List[float] = Field(min_length=9, max_length=9)
    mask: Optional[MaskUpload] = None

    @model_validator(mode="after")
    def _stride_holds_a_row(self) -> "PhoneFrame":
        if self.depth_row_bytes < self.depth_width * 4:
            raise ValueError("a depth row is at least depth_width float32s")
        return self


class PhoneMeasureInput(BaseModel):
    tenant_id: UUID
    frames: List[PhoneFrame]


class PhoneMeasured(BaseModel):
    """What the sequence made of one capture."""

    capture_id: UUID
    status: Literal[
        "measured", "no_fish", "headtail_failed", "depth_failed",
        "image_unavailable", "decode_failed",
    ]  # fmt: skip
    length_m: Optional[float] = Field(default=None, gt=0)
    snout_x: Optional[float] = None
    snout_y: Optional[float] = None
    fork_x: Optional[float] = None
    fork_y: Optional[float] = None
    snout_depth_m: Optional[float] = None
    fork_depth_m: Optional[float] = None
    #: The segmentation weights, ``fishial/<version>@<sha>``.
    model_id: Optional[str] = None
    core_version: Optional[str] = None
    algorithm: str = PHONE_ALGORITHM
    mask: Optional[StoredMask] = None
    error: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "PhoneMeasured":
        points = (self.snout_x, self.snout_y, self.fork_x, self.fork_y)
        if self.status == "measured" and (
            self.length_m is None or any(p is None for p in points)
        ):
            raise ValueError("a measured result names its length, snout and fork")
        if self.mask is not None and self.status not in PHONE_MASK_STATUSES:
            raise ValueError(f"a {self.status} result kept no mask to store")
        return self
