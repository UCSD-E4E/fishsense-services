"""The fish to classify: the head/tail stage's kept mask's box, padded.

New in v2 (no v1 counterpart). Ported from coral-gardeners-fish-detector@
67c8627 src/coral_fish_pipeline/utils/boxes.py (`expand_box_xyxy`,
`clip_box_xyxy`) and src/coral_fish_pipeline/cropping/cropper.py
(`create_crops`: the padded box rounded to whole pixels, cut from the RGB
frame). The padding and floor are the contract's (`SPECIES_CROP_PADDING`,
`SPECIES_CROP_MIN_SIZE`, its configs/default.yaml `crop:`).

v2 changes: the box is the one SAM 3.1 kept at the laser dot in the head/tail
stage, so there is no second segmentation; the frame is that stage's rendered
JPEG, the rectified frame the box is in; the crop stays in memory (the cropper
wrote it as a quality-95 JPEG and the classifier read it back).
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

import numpy as np
from PIL import Image

from fishsense_services_contracts.species_prediction import (
    SPECIES_CROP_MIN_SIZE,
    SPECIES_CROP_PADDING,
)

__all__ = ["clip_box", "crop_fish", "expand_box"]


def clip_box(box: Iterable[float], width: int, height: int) -> list[float]:
    """coral-gardeners' `clip_box_xyxy`: inside the frame, and never empty."""
    x1, y1, x2, y2 = map(float, box)
    x1 = max(0.0, min(x1, width - 1))
    y1 = max(0.0, min(y1, height - 1))
    x2 = max(0.0, min(x2, width))
    y2 = max(0.0, min(y2, height))
    if x2 <= x1:
        x2 = min(float(width), x1 + 1.0)
    if y2 <= y1:
        y2 = min(float(height), y1 + 1.0)
    return [x1, y1, x2, y2]


def expand_box(
    box: Iterable[float], padding: float, width: int, height: int, min_size: int = 1
) -> list[float]:
    """coral-gardeners' `expand_box_xyxy`: grow by `padding` of the box's size
    on every side, then to at least `min_size` about its centre, then clip."""
    x1, y1, x2, y2 = map(float, box)
    pad_x = max(1.0, x2 - x1) * padding
    pad_y = max(1.0, y2 - y1) * padding
    nx1, ny1, nx2, ny2 = x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    if nx2 - nx1 < min_size:
        nx1, nx2 = cx - min_size / 2.0, cx + min_size / 2.0
    if ny2 - ny1 < min_size:
        ny1, ny2 = cy - min_size / 2.0, cy + min_size / 2.0
    return clip_box([nx1, ny1, nx2, ny2], width, height)


def crop_fish(jpeg_bytes: bytes, box: Sequence[int]) -> Optional[Image.Image]:
    """The padded box cut from the frame, as an RGB image; None when the
    bytes do not decode."""
    # pylint: disable=import-outside-toplevel
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = (
        int(round(v))
        for v in expand_box(
            box, SPECIES_CROP_PADDING, width, height, SPECIES_CROP_MIN_SIZE
        )
    )
    rgb = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
    return Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")
