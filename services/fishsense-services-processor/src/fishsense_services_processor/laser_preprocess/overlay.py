"""Stage 0.1's pure-logic core: draw the expected-laser region, encode a JPEG.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-
processing-workflow-worker/src/fishsense_data_processing_workflow_worker/
activities/preprocess_laser_image.py (`overlay_laser_region_and_encode_jpeg`,
`overlay_laser_bbox_and_encode_jpeg`, `_rectify_overlay_encode`).

The region is a convex polygon as of 2026-08-27. The rectangle path is still
here: the two sides deploy independently, so a payload from an orchestrator
that predates the polygon arrives with `region=None` and has to keep
rendering. It is also what the notebook-parity test holds byte for byte, so
it is left exactly as it was rather than re-expressed as a 4-vertex polygon --
`cv2.rectangle` and `cv2.polylines` do not agree pixel for pixel at corners.

v2 change: the intrinsics are fishsense-core's own `CameraIntrinsics` (core
#83), not the v1 API SDK's, and the raw frame is a file the object store
streamed (`RawImage` takes a path or bytes alike).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

from fishsense_services_processor.jpeg import encode_jpeg as _encode_jpeg

__all__ = [
    "overlay_laser_bbox_and_encode_jpeg",
    "overlay_laser_region_and_encode_jpeg",
    "rectify_overlay_encode",
]

Bbox = Tuple[int, int, int, int]  # (x1, y1, x2, y2)
Region = Sequence[Sequence[int]]  # [[x, y], ...] convex, in draw order


def overlay_laser_region_and_encode_jpeg(
    rectified_bgr: np.ndarray, region: Region
) -> bytes:
    """Draw a 2-px green closed outline through `region` and encode the result
    to JPEG bytes. Does not mutate the input. An outline, never a fill: the
    labeler has to see the image under it."""
    img = rectified_bgr.copy()
    points = np.asarray(region, dtype=np.int32).reshape(-1, 1, 2)
    cv2.polylines(img, [points], True, (0, 255, 0), 2)
    return _encode_jpeg(img)


def overlay_laser_bbox_and_encode_jpeg(rectified_bgr: np.ndarray, bbox: Bbox) -> bytes:
    """Draw a 2-px green rectangle at `bbox` and encode the result to JPEG
    bytes. Does not mutate the input. The pre-polygon shape, kept as the
    version-skew fallback."""
    img = rectified_bgr.copy()
    x1, y1, x2, y2 = bbox
    cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
    return _encode_jpeg(img)


def rectify_overlay_encode(
    raw: Path | bytes,
    camera_matrix: list[list[float]],
    distortion_coefficients: list[float],
    bbox: Bbox,
    region: Optional[Region] = None,
) -> bytes:
    """Heavy CPU work, run off the event loop: rawpy decode, `cv2.undistort`,
    CLAHE, then the overlay. `region` wins when present; `bbox` is the
    fallback for a payload that predates the polygon."""
    # pylint: disable=import-outside-toplevel
    from fishsense_core.camera_intrinsics import CameraIntrinsics
    from fishsense_core.image.raw_image import RawImage
    from fishsense_core.image.rectified_image import RectifiedImage

    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion_coefficients=np.array(distortion_coefficients, dtype=float),
    )
    rectified = RectifiedImage(RawImage(raw), intrinsics)
    if region:
        return overlay_laser_region_and_encode_jpeg(rectified.data, region)
    return overlay_laser_bbox_and_encode_jpeg(rectified.data, bbox)
