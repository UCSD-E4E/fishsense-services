"""The frame the slate detector sees: rendered as its training frames were.

Ported from 2026-10-03_slate_detector@95a77d95 src/slate_detector/render.py
(`render_rgb`, `render_to`): fishsense-core's `RawImage` with
`DecodeConfig.production()`, then `RectifiedImage` with the camera's
intrinsics (how fishsense-lite and fishsense-detector render frames), as RGB.
The model was trained, cross-validated and scanned on frames read back from
that repo's cache -- shrunk to 1600 px on the long side (Lanczos) and saved as
quality-95 JPEG -- so the same shrink and round trip happen here before the
model's own resize to 1024x768. Never the camera's embedded JPEG preview: it
is rendered differently and is not rectified.

v2 change: the raw is the staged copy the orchestrator issued, streamed to a
temporary file, rather than the source repo's local mirror of the NAS.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

import numpy as np
import rawpy
from fishsense_core.camera_intrinsics import CameraIntrinsics
from fishsense_core.image.decode import DecodeConfig
from fishsense_core.image.raw_image import RawImage
from fishsense_core.image.rectified_image import RectifiedImage
from PIL import Image

__all__ = [
    "CACHE_LONG_SIDE",
    "DECODE_ERRORS",
    "JPEG_QUALITY",
    "as_training_frame",
    "render_frame",
]

#: The source repo's frame cache: `render.CACHE_LONG_SIDE`, `JPEG_QUALITY`.
CACHE_LONG_SIDE = 1600
JPEG_QUALITY = 95

#: What a raw that will not decode raises (rawpy's, under fishsense-core):
#: the frame is an abstention, not a retry.
DECODE_ERRORS: tuple[type[BaseException], ...] = (rawpy.LibRawError,)


def as_training_frame(bgr: np.ndarray) -> Image.Image:
    """A rectified BGR frame as the model's training frames were read: RGB,
    shrunk to `CACHE_LONG_SIDE`, through a quality-`JPEG_QUALITY` JPEG."""
    image = Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]))
    image.thumbnail((CACHE_LONG_SIDE, CACHE_LONG_SIDE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")


def render_frame(
    raw: Path,
    camera_matrix: Sequence[Sequence[float]],
    distortion_coefficients: Sequence[float],
) -> Image.Image:
    """The detector's input for one raw: decoded, rectified, as trained."""
    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion_coefficients=np.array(distortion_coefficients, dtype=float),
    )
    bgr = RectifiedImage(
        RawImage(raw, config=DecodeConfig.production()), intrinsics
    ).data
    return as_training_frame(bgr)
