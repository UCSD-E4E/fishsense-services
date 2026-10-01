"""Geometry for the head/tail predict stage -- no model, no GPU, no I/O.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/
headtail_geometry.py. Kept separate from the activity because these are the
parts that fail *plausibly*: a wrong crop origin displaces every keypoint by a
constant and still produces a fish-shaped answer; a wrong silhouette ratio
quietly changes which predictions get seeded.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "crop_origin",
    "lift_point",
    "mask_at_point",
    "mask_box",
    "silhouette_ratio",
]


def crop_origin(
    laser_x: float,
    laser_y: float,
    frame_w: int,
    frame_h: int,
    crop_w: int,
    crop_h: int,
) -> Tuple[int, int]:
    """Top-left of the crop window centred on the laser dot, clamped inside
    the frame. Clamped rather than padded or truncated, which keeps the fish
    in view and the input shape constant."""
    max_x = max(0, frame_w - crop_w)
    max_y = max(0, frame_h - crop_h)
    ox = int(min(max(0, laser_x - crop_w // 2), max_x))
    oy = int(min(max(0, laser_y - crop_h // 2), max_y))
    return ox, oy


def lift_point(
    point: Sequence[float], origin_x: int, origin_y: int
) -> Tuple[float, float]:
    """Move a crop-local point back into rectified-frame coordinates -- the
    space the laser labels and a labeler's clicks are in."""
    return (float(point[0]) + origin_x, float(point[1]) + origin_y)


def mask_at_point(
    masks: Iterable[np.ndarray], points: Sequence[Sequence[float]]
) -> Optional[np.ndarray]:
    """The laser gate: the first mask whose pixel at a laser dot is set. Every
    dot is tried, first hit wins. None (an abstention, not an error) when no
    dot lands on any mask."""
    masks = list(masks)
    for px, py in points:
        xi, yi = int(round(px)), int(round(py))
        for mask in masks:
            if 0 <= yi < mask.shape[0] and 0 <= xi < mask.shape[1] and mask[yi, xi]:
                return mask
    return None


def silhouette_ratio(mask_area_px: int, length_px: float) -> Optional[float]:
    """`mask_area / length**2`: how fish-shaped a detection is (a real fish
    runs ~0.15-0.30). Recorded on every row and applied at seed time, so it can
    be retuned without re-predicting. None for a degenerate length."""
    if not length_px:
        return None
    return mask_area_px / (length_px * length_px)


def mask_box(binary: np.ndarray, origin_x: int, origin_y: int) -> Optional[list[int]]:
    """The mask's box, ``[x_min, y_min, x_max, y_max)``, lifted into frame
    pixels like the keypoints. New in v2 (no v1 counterpart): what the species
    pre-annotation stage crops by. None for an empty mask."""
    ys, xs = np.nonzero(binary)
    if xs.size == 0:
        return None
    return [
        int(xs.min()) + origin_x,
        int(ys.min()) + origin_y,
        int(xs.max()) + 1 + origin_x,
        int(ys.max()) + 1 + origin_y,
    ]
