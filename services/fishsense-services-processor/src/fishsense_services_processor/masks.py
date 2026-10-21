"""A kept SAM mask, stored as a file (contract 12, `contracts.masks`).

New in v2 (2026-10-21). The kernel (`headtail_predict.predict`) segments a crop
around the laser dot and used to drop the mask it kept once its head/tail,
box and area were taken. A `MaskSink` now holds it; the stage places it in a
frame-sized uint8 array -- the app's ``mask_u8``: width x height, row by row,
255 on the fish -- gzips it (no timestamp, so the same mask is the same
bytes), and PUTs it to the key the orchestrator presigned (`store_mask`),
naming what it stored. The orchestrator reads it back and checks it before
recording it.
"""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import numpy as np

from fishsense_services_contracts.masks import (
    MASK_CONTENT_TYPE,
    MaskUpload,
    StoredMask,
)

__all__ = ["FrameMask", "MaskSink", "store_mask"]

#: ``(url, headers, data) -> HTTP status``: `renders.activities.http_put`.
Put = Callable[[str, dict, bytes], int]


@dataclass(frozen=True)
class FrameMask:
    """A crop-local binary mask, where its crop sits in a frame."""

    crop: np.ndarray
    x: int
    y: int
    width: int
    height: int

    def encode(self) -> bytes:
        """The app's ``mask_u8`` at the frame, gzip'd."""
        frame = np.zeros((self.height, self.width), np.uint8)
        crop = np.asarray(self.crop) > 0
        h = max(0, min(crop.shape[0], self.height - self.y))
        w = max(0, min(crop.shape[1], self.width - self.x))
        frame[self.y : self.y + h, self.x : self.x + w][crop[:h, :w]] = 255
        return gzip.compress(frame.tobytes(), compresslevel=6, mtime=0)


class MaskSink:  # pylint: disable=too-few-public-methods
    """Where the kernel leaves the mask it kept, for its stage to store."""

    def __init__(self) -> None:
        self.mask: Optional[FrameMask] = None

    def keep(self, crop, x: int, y: int, width: int, height: int) -> None:
        self.mask = FrameMask(crop=crop, x=x, y=y, width=width, height=height)


def store_mask(put: Put, upload: MaskUpload, mask: FrameMask) -> StoredMask:
    """PUT the mask to its presigned key and name what was stored. A PUT
    the store refuses fails the frame, to be retried, never recorded."""
    data = mask.encode()
    status = put(upload.put_url, {"Content-Type": MASK_CONTENT_TYPE}, data)
    if not 200 <= status < 300:
        raise RuntimeError(f"PUT of mask {upload.key} answered {status}")
    return StoredMask(
        key=upload.key,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=mask.width,
        height=mask.height,
    )
