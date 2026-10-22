"""SAM 3.1 on a slate frame's render, as cscw's sam_sizes.py does it.

Ported from cscw-fishsense2027@8312780a
`calibration_analysis/e1j_size_constancy/sam_sizes.py` (:33-66) and the render
cache it read (2026-10-03_slate_detector `render.py`: the rectified frame,
``thumbnail((1600, 1600), LANCZOS)``, JPEG quality 95):

- each dot maps to the render as ``int(round(dot * scale))``;
- the prompts ``"slate"`` then ``"white board"`` are tried in order, and a dot
  takes the highest-scoring mask containing it under the first prompt that
  has one;
- its size is sqrt(area), in render px.

v2: the render is the capture's own (tenant bucket, presigned GET) at full
resolution, downscaled here exactly as the cache was; SAM runs on a processor
of its own over the shared model (`Sam3Processor(model, confidence_threshold=
0.3)`), so the head/tail stages' processor keeps its 0.5.
"""

from __future__ import annotations

import io
from typing import Callable, List, Sequence, Tuple

import numpy as np

from fishsense_services_contracts.shadow_calibration import (
    SHADOW_RENDER_LONG_SIDE,
    SHADOW_SAM_PROMPTS,
    DotSegment,
)

__all__ = ["downscaled", "segment_dots"]

#: ``(image RGB, prompt) -> [(mask (H, W) bool, score)]``.
Segmenter = Callable[[np.ndarray, str], List[Tuple[np.ndarray, float]]]
_JPEG_QUALITY = 95


def downscaled(jpeg: bytes, long_side: int = SHADOW_RENDER_LONG_SIDE):
    """``(RGB uint8 array, full-res px per render px)``: the render as the
    port's cache held it."""
    # pylint: disable-next=import-outside-toplevel
    from PIL import Image

    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    full = max(image.size)
    image.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    cached = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    return np.asarray(cached), full / max(cached.size)


def segment_dots(
    image: np.ndarray,
    scale: float,
    dots: Sequence[Sequence[float]],
    segmenter: Segmenter,
    prompts: Sequence[str] = SHADOW_SAM_PROMPTS,
) -> List[DotSegment]:
    """The mask under each dot (full-res px), as sam_sizes.py picks it."""
    h, w = image.shape[:2]
    pixels = [(int(round(x / scale)), int(round(y / scale))) for x, y in dots]
    found: dict[int, DotSegment] = {}
    for prompt in prompts:
        if len(found) == len(dots):
            break
        pairs = segmenter(image, prompt)
        for k, (x, y) in enumerate(pixels):
            if k in found or not (0 <= y < h and 0 <= x < w):
                continue
            hits = [(m, s) for m, s in pairs if m[y, x]]
            if hits:
                m, s = max(hits, key=lambda p: p[1])
                area = float(np.count_nonzero(m))
                found[k] = DotSegment(
                    prompt=prompt, score=float(s), mask_area_px=area, size=area**0.5
                )
    return [found.get(k, DotSegment()) for k in range(len(dots))]
