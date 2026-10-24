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

2026-10-24: the gate is fishsense-core's (`find_fish_at_dot(...,
grow_cut_masks=True, score_gate=AUTOMATIC_SAM_SCORE_GATE)`, core #93): masks
under it are dropped before the laser gate, on both passes, and the kept
mask's score comes back on `FishAtDot.score`. That is the rule this module's
`_Gated` wrapper applied, so outputs are unchanged. Under a gate core always
reports the kept mask's score, and refuses a SAM build that returns none on
its first frame (`ValueError`), so `mask_unscored` is no longer produced.
`ScoredSam3Adapter` keeps only `encoded` / `prompted` (several prompts on one
encoding), which core did not port and the shadow calibration uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import numpy as np
from fishsense_core.fish.fish_at_dot import to_numpy

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AUTOMATIC_SAM_SCORE_GATE,
    AutomaticFrameResult,
)
from fishsense_services_processor.headtail_predict.predict import (
    PredictOptions,
    find_fish_in_jpeg,
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
    mask_sink: Any = None,
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

    found = find_fish_in_jpeg(
        jpeg_bytes,
        [[dot.x, dot.y]],
        segmenter,
        PredictOptions(
            checkpoint=checkpoint,
            core_version=core_version,
            predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
            grow_cut_masks=True,
        ),
        mask_sink=mask_sink,
        score_gate=gate,
    )
    if found is None:
        return AutomaticFrameResult(status="decode_failed", **base)
    head = found.head or (None, None)
    tail = found.tail or (None, None)
    return AutomaticFrameResult(
        status=found.status,
        head_x=head[0],
        head_y=head[1],
        tail_x=tail[0],
        tail_y=tail[1],
        width=found.width,
        height=found.height,
        mask_area_px=found.mask_area_px,
        silhouette_ratio=found.silhouette_ratio,
        crop_x=found.crop_x,
        crop_y=found.crop_y,
        mask_bbox=list(found.mask_bbox) if found.mask_bbox is not None else None,
        sam_score=found.score,
        checkpoint=checkpoint,
        **base,
    )


def _device_type() -> str:
    # pylint: disable-next=import-outside-toplevel,import-error
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


class ScoredSam3Adapter:
    """SAM 3.1 asked several prompts on one encoding (bfloat16 autocast as a
    context manager, a PIL image): the shadow calibration's (review of #261).
    The chain's own scored segmentation is core's `Sam3Segmenter`."""

    def __init__(self, processor: Any):
        self._processor = processor

    def encoded(self, image_bgr: np.ndarray) -> Any:
        """The image through SAM's encoder once: `prompted` asks it each
        prompt (the shadow calibration's two; review of #261)."""
        # pylint: disable=import-outside-toplevel,import-error
        import cv2
        import PIL.Image
        import torch

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        with torch.autocast(_device_type(), dtype=torch.bfloat16):
            return self._processor.set_image(PIL.Image.fromarray(rgb))

    def prompted(self, state: Any, prompt: str) -> List[Tuple[np.ndarray, float]]:
        # pylint: disable-next=import-outside-toplevel,import-error
        import torch

        with torch.autocast(_device_type(), dtype=torch.bfloat16):
            if hasattr(self._processor, "reset_all_prompts"):
                self._processor.reset_all_prompts(state)
            state = self._processor.set_text_prompt(prompt, state)
        if not hasattr(state, "get") or state.get("masks") is None:
            return []
        scores = state.get("scores")
        masks = [to_numpy(m).squeeze() for m in state["masks"]]
        if scores is None:
            raise ValueError("SAM 3.1 returned masks without scores")
        if getattr(scores, "float", None) is not None:
            scores = scores.float()  # numpy has no bfloat16
        return list(zip(masks, (float(s) for s in to_numpy(scores).reshape(-1))))
