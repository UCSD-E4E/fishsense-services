"""One frame of the automatic chain: the dot, the SAM 3.1 mask at it, the
head/tail -- no model loading, no I/O.

The chain is cscw-fishsense2027@96a8da07 e2e_measurement/run_e2e.py
(`laser_pass`, then `headtail_pass`'s ``ht_auto``: production's
`predict_from_jpeg` seeded by the **automatic** dot). The mask, the gate and
the keypointer are the head/tail stage's own kernel
(`headtail_predict.predict.predict_from_jpeg`: the 1800x1350 crop around the
dot, the dot must be on the mask, `FishHeadTailDetector`), so nothing is
re-implemented; what this adds is

* **the SAM score as the gate, explicitly** (paper §6.3): the segmenter yields
  ``(mask, score)`` and only masks scoring at least `AUTOMATIC_SAM_SCORE_GATE`
  reach the kernel; the kept mask's score is recorded. Production's
  `Sam3Processor` already drops masks under its own 0.5 threshold; pinning the
  gate here makes it the stage's, not a library default's;
* **slate frames** keep their dot for the calibration and are never
  segmented as fish.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AUTOMATIC_SAM_SCORE_GATE,
    AutomaticFrameResult,
)
from fishsense_services_processor.headtail_predict.predict import (
    PredictOptions,
    _to_numpy,
    predict_from_jpeg,
)

__all__ = ["LaserDot", "ScoredSam3Adapter", "automatic_frame_result"]


@dataclass(frozen=True)
class LaserDot:
    """The detector's dot, in rectified px, and what found it."""

    x: float
    y: float
    confidence: float
    predictor_version: int
    checkpoint: Optional[str]


class _Gated:
    """A `segment` seam over a scored segmenter: only masks at or above the
    gate pass, and their scores are kept to name the kept one's."""

    def __init__(self, scored: Any, gate: float):
        self._scored = scored
        self._gate = gate
        self.kept: List[Tuple[np.ndarray, float]] = []

    def segment(self, image_bgr: np.ndarray) -> List[np.ndarray]:
        self.kept = [
            (np.asarray(m), float(s))
            for m, s in self._scored.segment_scored(image_bgr)
            if float(s) >= self._gate
        ]
        return [m for m, _ in self.kept]

    def score_at(self, local_point: Sequence[float]) -> Optional[float]:
        """The kernel's choice, by its own rule: the first kept mask whose
        pixel at the dot is set."""
        xi, yi = int(round(local_point[0])), int(round(local_point[1]))
        for mask, score in self.kept:
            m = np.asarray(mask)
            if 0 <= yi < m.shape[0] and 0 <= xi < m.shape[1] and m[yi, xi]:
                return score
        return None


def automatic_frame_result(
    *,
    capture_id: uuid.UUID,
    dot: Optional[LaserDot],
    jpeg_bytes: bytes,
    segmenter: Any,
    checkpoint: Optional[str],
    core_version: Optional[str],
    is_slate: bool = False,
    slate_probability: Optional[float] = None,
    gate: float = AUTOMATIC_SAM_SCORE_GATE,
) -> AutomaticFrameResult:
    """One frame's automatic dot and head/tail (or which abstention)."""
    base = {
        "capture_id": capture_id,
        "predictor_version": AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        "slate_probability": slate_probability,
        "core_version": core_version,
    }
    if dot is None:
        return AutomaticFrameResult(status="no_laser_dot", **base)
    base.update(
        laser_x=dot.x,
        laser_y=dot.y,
        laser_confidence=dot.confidence,
        laser_predictor_version=dot.predictor_version,
        laser_checkpoint=dot.checkpoint,
    )
    if is_slate:
        return AutomaticFrameResult(status="slate_frame", **base)

    gated = _Gated(segmenter, gate)
    r = predict_from_jpeg(
        jpeg_bytes,
        [[dot.x, dot.y]],
        gated,
        capture_id,
        None,
        PredictOptions(
            checkpoint=checkpoint,
            core_version=core_version,
            predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        ),
    )
    kept = r.status in ("predicted", "headtail_failed")
    score = (
        gated.score_at((dot.x - r.crop_x, dot.y - r.crop_y))
        if kept and r.crop_x is not None
        else None
    )
    return AutomaticFrameResult(
        status=r.status,
        head_x=r.head_x,
        head_y=r.head_y,
        tail_x=r.tail_x,
        tail_y=r.tail_y,
        width=r.width,
        height=r.height,
        mask_area_px=r.mask_area_px,
        silhouette_ratio=r.silhouette_ratio,
        crop_x=r.crop_x,
        crop_y=r.crop_y,
        mask_bbox=r.mask_bbox,
        sam_score=score,
        checkpoint=checkpoint,
        **base,
    )


class ScoredSam3Adapter:  # pylint: disable=too-few-public-methods
    """The SAM 3.1 processor behind a scored seam: the head/tail stage's
    `_Sam3Adapter` (bfloat16 autocast as a context manager, a PIL image,
    prompt "fish"), returning each mask with its score."""

    def __init__(self, processor: Any, prompt: str = "fish"):
        self._processor = processor
        self._prompt = prompt

    def segment_scored(self, image_bgr: np.ndarray) -> List[Tuple[np.ndarray, float]]:
        # pylint: disable=import-outside-toplevel,import-error
        import cv2
        import PIL.Image
        import torch

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        device_type = "cuda" if torch.cuda.is_available() else "cpu"
        with torch.autocast(device_type, dtype=torch.bfloat16):
            state = self._processor.set_image(PIL.Image.fromarray(rgb))
            state = self._processor.set_text_prompt(self._prompt, state)
        if not hasattr(state, "get") or state.get("masks") is None:
            return []
        scores = state.get("scores")
        masks = [_to_numpy(m).squeeze() for m in state["masks"]]
        if scores is None:
            raise ValueError("SAM 3.1 returned masks without scores")
        if getattr(scores, "float", None) is not None:
            scores = scores.float()  # numpy has no bfloat16
        return list(zip(masks, (float(s) for s in _to_numpy(scores).reshape(-1))))
