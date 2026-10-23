"""One frame through fishsense-core 4.2.0's SimCC snout/fork model, read-only
(owner, 2026-10-23: compare it with the SAM rule before any switch).

The frame is the render the automatic prediction was made on, decoded; the box
is that prediction's SAM mask box, from which core cuts the model's crop
(`crop_from_box`: the crop rule is part of the model). The model runs twice:

- on the decoded render (`HeadTailKeypointDetector.predict`), as production
  would feed it;
- on the same crop JPEG-encoded again at quality 95 (``rt_*``), as the
  experiment's crops were (`sam_boxes.py`'s ``cv2.imencode``): core's docstring
  calls that input gap unmeasured, and this measures it.

A box the model refuses (outside the frame: an empty crop) is ``refused``.
"""

from __future__ import annotations

from typing import Any, Sequence

import cv2
import numpy as np

__all__ = ["RT_JPEG_QUALITY", "keypoints_frame"]

RT_JPEG_QUALITY = 95


def keypoints_frame(jpeg: bytes, mask_bbox: Sequence[int], detector: Any) -> dict:
    """The `KeypointResult` fields (without ids) for one frame. Blocking.
    Raises `ValueError` if the render does not decode."""
    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("the render does not decode")
    try:
        kp = detector.predict(frame, list(mask_bbox))
    except ValueError:
        return {"status": "refused"}
    x0, y0, x1, y1 = kp.crop
    ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(frame[y0:y1, x0:x1]),
                           [cv2.IMWRITE_JPEG_QUALITY, RT_JPEG_QUALITY])  # fmt: skip
    if not ok:
        raise ValueError("the crop would not encode")
    crop = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    points, confidence = detector.predict_crops([crop])
    (hx, hy), (tx, ty) = points[0]
    return {
        "status": "predicted",
        "head": [float(kp.head[0]), float(kp.head[1])],
        "tail": [float(kp.tail[0]), float(kp.tail[1])],
        "confidence": float(kp.confidence),
        "crop": [int(x0), int(y0), int(x1), int(y1)],
        "rt_head": [float(hx) + x0, float(hy) + y0],
        "rt_tail": [float(tx) + x0, float(ty) + y0],
        "rt_confidence": float(confidence[0]),
    }
