"""A phone capture measured the way FishSense Mobile measures it.

Ported line for line from fishsense-mobile@cd120f4 rust-bridge/src/lib.rs
(`do_compute_length`, `do_recompute_length`), which pins fishsense-core
4.1.0. The owner, 2026-10-21: replicate the app, not the OCEANS 2025 paper's
notebook (its flood fill and its row/column bug are not reproduced).
The owner, 2026-11-09: "let's just port for now"; moving the sequence to one
place both sides call is #475.

The sequence, as the app runs it:

1. `FishSegmentation.inference_single` on the BGR frame: one fish, ranked by
   score x area, as a 0/255 mask; none is "no fish", not an error;
2. `FishHeadTailDetector.find_head_tail_img` on the mask: snout and fork;
3. `predict_keypoint_depths`: a RANSAC plane fit in the mask, in camera
   space, for each point's depth (robust to the 1920x1440 frame against the
   256x192 ARKit depth, holes in the depth, and a fish lying on a board);
4. `WorldPointHandler.compute_world_point_from_depth` for each point; the
   length is the distance between them.

`remeasure` is the app's recompute after an edit: steps 3 and 4 only, on the
mask as it was and the person's snout and fork, which are never moved.

v2 differences, none in the method:

* fishsense-core is the processor's 4.3.2, not 4.1.0. Between the two tags
  the Rust core changed only its changelog, version and model manifest (new
  entries; `fishial` unchanged), and the bindings' Rust not at all; the
  Python `WorldPointHandler` only gained laser code (checked 2026-11-09);
* the frame is the stored JPEG (`rgb_jpeg`), decoded to BGR, where the app
  converts its in-memory RGBA. Its EXIF orientation is ignored: the app
  segments the camera's own grid, the one the depth map and intrinsics are
  in (CCFRP's are orientation 1, 1920x1440, checked 2026-11-09);
* the depth buffer is read with its row stride (`depth_row_bytes`), which
  ARKit pads and the app copies;
* the app takes K⁻¹ from Swift; here it is the stored K, inverted, in
  float32 as the app's FFI passes it. The world point wrapper computes in
  float64 where the app's Rust uses float32.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

import cv2
import numpy as np
from fishsense_core.fish import FishHeadTailDetector
from fishsense_core.world_point import WorldPointHandler

__all__ = ["PhoneMeasurement", "decode_bgr", "measure", "read_depth", "remeasure"]

Point = Tuple[float, float]


@dataclass(frozen=True)
class PhoneMeasurement:
    #: ``measured``; ``no_fish`` (segmentation kept none); ``headtail_failed``
    #: (no snout and fork from the mask); ``depth_failed`` (the plane fit and
    #: its fallback found no depth at a point).
    status: str
    length_m: Optional[float] = None
    snout: Optional[Point] = None
    fork: Optional[Point] = None
    snout_depth_m: Optional[float] = None
    fork_depth_m: Optional[float] = None
    #: (H, W) uint8, 0 or 255, in the frame's grid.
    mask: Optional[np.ndarray] = None
    #: The segmentation weights, `fishial/<version>@<sha>`; None for a remeasure.
    model_id: Optional[str] = None
    error: Optional[str] = None


def decode_bgr(data: bytes) -> Optional[np.ndarray]:
    """The JPEG's pixels as BGR, in the grid they were captured in."""
    flags = cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
    return cv2.imdecode(np.frombuffer(data, np.uint8), flags)


def read_depth(data: bytes, width: int, height: int, row_bytes: int) -> np.ndarray:
    """The depth map, meters, float32 (height, width), without ARKit's row pad."""
    if row_bytes < width * 4 or len(data) != row_bytes * height:
        raise ValueError(
            f"depth is {len(data)} bytes, not {height} rows of {row_bytes} "
            f"for {width} float32 columns"
        )
    rows = np.frombuffer(data, np.uint8).reshape(height, row_bytes)
    return np.ascontiguousarray(rows[:, : width * 4]).view(np.float32)


def _k_inv(intrinsics: Sequence[float]) -> np.ndarray:
    return np.linalg.inv(np.asarray(intrinsics, np.float64).reshape(3, 3)).astype(
        np.float32
    )


def _length(mask, depth, k_inv, snout, fork, **extra) -> PhoneMeasurement:
    """Steps 3 and 4, shared by `measure` and `remeasure` as by the app's two."""
    detector = FishHeadTailDetector()
    s = np.asarray(snout, np.float32)
    f = np.asarray(fork, np.float32)
    try:
        head_depth, tail_depth = detector.predict_keypoint_depths(
            mask, depth, k_inv, s, f
        )
    except ValueError as exc:
        return PhoneMeasurement("depth_failed", snout=snout, fork=fork, mask=mask,
                                error=str(exc), **extra)  # fmt: skip
    handler = WorldPointHandler(k_inv)
    head_3d = handler.compute_world_point_from_depth(s, head_depth)
    tail_3d = handler.compute_world_point_from_depth(f, tail_depth)
    diff = np.asarray(head_3d) - np.asarray(tail_3d)
    return PhoneMeasurement(
        "measured", length_m=float(np.sqrt(diff.dot(diff))), snout=snout, fork=fork,
        snout_depth_m=float(head_depth), fork_depth_m=float(tail_depth), mask=mask,
        **extra,
    )  # fmt: skip


def measure(
    bgr: np.ndarray, depth: np.ndarray, intrinsics: Sequence[float], *, segmenter: Any
) -> PhoneMeasurement:
    """The app's `compute_length`, on one frame. `segmenter` is a loaded
    `FishSegmentation` (or anything with its `inference_single` and
    `model_id`)."""
    model_id = segmenter.model_id()
    mask = segmenter.inference_single(bgr)
    if mask is None:
        return PhoneMeasurement("no_fish", model_id=model_id)
    try:
        head, tail = FishHeadTailDetector().find_head_tail_img(mask)
    except ValueError as exc:
        return PhoneMeasurement("headtail_failed", mask=mask, model_id=model_id,
                                error=str(exc))  # fmt: skip
    snout = (float(head[0]), float(head[1]))
    fork = (float(tail[0]), float(tail[1]))
    return _length(mask, depth, _k_inv(intrinsics), snout, fork, model_id=model_id)


def remeasure(
    mask: np.ndarray,
    depth: np.ndarray,
    intrinsics: Sequence[float],
    snout: Point,
    fork: Point,
) -> PhoneMeasurement:
    """The app's `recompute_length`: the length at the given snout and fork,
    on the mask as it was."""
    return _length(mask, depth, _k_inv(intrinsics), snout, fork)
