"""The one JPEG encoder every processor stage writes with.

Ported from fishsense-lite@a8b2c3bc workflow-worker
activities/preprocess_headtail_image.py (`encode_rectified_jpeg`), which v1
repeated in the laser, species and checkerboard stages. OpenCV's default
quality, so a JPEG v2 writes matches v1's for the same pixels.
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["encode_jpeg"]


def encode_jpeg(image_bgr: np.ndarray) -> bytes:
    """Encode a BGR ndarray as JPEG bytes. Does not mutate it."""
    success, encoded = cv2.imencode(".jpg", image_bgr)
    if not success:
        raise RuntimeError("cv2.imencode failed")
    return encoded.tobytes()
