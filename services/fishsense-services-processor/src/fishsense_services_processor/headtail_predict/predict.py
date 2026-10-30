"""Head/tail prediction: a SAM 3.1 mask of the lasered fish, keypointed.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
predict_headtail_image.py (everything but the activity, which is in
`activities`). Behavior is v1's, and three of its choices are measured:

* **read the stage-5.1 JPEG**, the exact frame the labeler is shown;
* **crop, don't tile**: one 1800x1350 window centered on the *first* laser dot,
  clamped inside the frame; the gate tries every dot, first hit wins;
* **SAM 3.1** (prompt "fish"), with fishsense-core's Mask R-CNN
  (`FishSegmentation`) as the fallback when there is no GPU.

v1's hard-won rules, kept: SAM 3.1 cannot be built without CUDA, and that is a
non-retryable `NoGpuForSam3`; it runs under `torch.autocast(bfloat16)` as a
context manager and is fed a PIL image; tensors are detached and moved to the
CPU before numpy; the fallback is loaded before it is published, fed BGR,
landscape crops only, and its instance map is split into binary masks; model
singletons use a double-checked lock (activities run in a thread pool).

v2 changes: ids are UUIDs; SAM 3.1 is built through fishsense-core's
`fish.sam3.build_segmenter` (whose own `Sam3RequiresGpu` is mapped to the same
non-retryable error); the version constants come from the contract package;
**an abstention names the dot it came from** (the dot on the kept mask for
`headtail_failed`, else the crop's center, the first), where v1 left it NULL
and a corrected dot never made the row stale; **a kept mask's box rides on the
result** (`mask_bbox`, frame pixels), which the species pre-annotation stage
crops by.

2026-10-24: the kernel is fishsense-core's (`fishsense_core.fish.fish_at_dot`,
core #89, ported from this module): the crop, the laser gate, the keypointer,
the whole-frame retry of a cut mask and SAM's score gate, with core's
`Sam3Segmenter` and `FishialSegmenter` in place of this module's adapters.
What stays here is the service's: decoding the JPEG (`decode_failed`), the
models' loading and GPU refusal, naming a laser *label* from the dot core
names, and the result row. Core's `FishialSegmenter` has no portrait refusal
(core's native code has rotated portrait input since 4.0.0), which this
module's adapter kept and turned into `no_detections`; the stage's crops are
1800x1350 windows of landscape frames, so its outputs do not change and
`HEADTAIL_PREDICTOR_VERSION` stands.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np
from fishsense_core.fish.fish_at_dot import FishAtDot, find_fish_at_dot
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.headtail import (
    HEADTAIL_CROP_HEIGHT,
    HEADTAIL_CROP_WIDTH,
    HEADTAIL_PREDICTOR_VERSION,
    HeadtailPredictionResult,
)

__all__ = [
    "FALLBACK_CHECKPOINT",
    "PredictOptions",
    "cuda_available",
    "find_fish_in_jpeg",
    "get_fallback_segmenter",
    "get_segmenter",
    "predict_from_jpeg",
]

_log = logging.getLogger(__name__)

#: What a fallback-tier row records as its checkpoint (v1's): the weights are
#: embedded in fishsense-core's native module.
FALLBACK_CHECKPOINT = "fishsense_core.fish.FishSegmentation"

# The weights load once per process. The lock is load-bearing: a cold pod's
# first batch enters together from the activity thread pool.
_SEGMENTER: Any = None
_SEGMENTER_LOCK = threading.Lock()
_FALLBACK_SEGMENTER: Any = None
_FALLBACK_LOCK = threading.Lock()


def cuda_available() -> bool:
    """Whether this process has a usable CUDA device. A broken CUDA runtime,
    or no torch at all, reads as "no GPU"."""
    try:
        import torch  # pylint: disable=import-outside-toplevel,import-error

        return bool(torch.cuda.is_available())
    except Exception:  # pylint: disable=broad-except
        _log.debug("no usable CUDA device; head/tail predict will use fallback")
        return False


def _no_gpu() -> ApplicationError:
    # Retrying cannot help: the pod will not grow a GPU. Left retryable it
    # loops until the workflow times out while holding the pod (dive 94).
    return ApplicationError(
        "SAM 3.1 requires a GPU: build_sam3_image_model allocates its "
        "position-encoding cache on a hardcoded device='cuda'. This worker has "
        "no usable CUDA device.",
        type="NoGpuForSam3",
        non_retryable=True,
    )


def _load_segmenter(checkpoint_path: str) -> Any:
    """Build the SAM 3.1 concept segmenter from a verified checkpoint.

    Loading prints four missing keys (`backbone.vision_backbone.convs.3.*`);
    they are benign -- that conv is never trained or used. Any other set is a
    red flag: upstream loads with `strict=False`, which is why the file was
    verified by hash before it got here.
    """
    if not cuda_available():
        raise _no_gpu()
    # pylint: disable=import-outside-toplevel
    from fishsense_core.fish.sam3 import Sam3RequiresGpu, build_segmenter

    try:
        return build_segmenter(checkpoint_path)
    except Sam3RequiresGpu as exc:
        raise _no_gpu() from exc


def get_segmenter(checkpoint_path: str) -> Any:
    """The process-wide SAM 3.1 processor, loaded on first use."""
    global _SEGMENTER  # pylint: disable=global-statement
    if _SEGMENTER is not None:
        return _SEGMENTER
    with _SEGMENTER_LOCK:
        if _SEGMENTER is None:
            _log.info("loading SAM3 checkpoint=%s", checkpoint_path)
            _SEGMENTER = _load_segmenter(checkpoint_path)
    return _SEGMENTER


def get_fallback_segmenter() -> Any:
    """The process-wide `FishSegmentation`, loaded before it is published:
    unloaded, `inference` raises a retryable error with no ceiling."""
    global _FALLBACK_SEGMENTER  # pylint: disable=global-statement
    if _FALLBACK_SEGMENTER is not None:
        return _FALLBACK_SEGMENTER
    with _FALLBACK_LOCK:
        if _FALLBACK_SEGMENTER is None:
            # pylint: disable=import-outside-toplevel,import-error,no-name-in-module
            from fishsense_core.fish import FishSegmentation

            _log.info("loading fishsense-core FishSegmentation (fallback backend)")
            segmentation = FishSegmentation()
            segmentation.load_model()
            _FALLBACK_SEGMENTER = segmentation
    return _FALLBACK_SEGMENTER


@dataclass(frozen=True)
class PredictOptions:
    """Window size and provenance for one prediction."""

    crop_w: int = HEADTAIL_CROP_WIDTH
    crop_h: int = HEADTAIL_CROP_HEIGHT
    checkpoint: Optional[str] = None
    core_version: Optional[str] = None
    #: The tier: the stage's version for SAM 3.1, the fallback's for Mask R-CNN.
    predictor_version: int = HEADTAIL_PREDICTOR_VERSION
    #: A kept mask cut by an edge of the window that is not the frame's is
    #: segmented again on the whole frame (`cut_by_window`). The automatic
    #: chain's (2026-10-22); off for the human path, whose pre-fill and
    #: version are unchanged.
    grow_cut_masks: bool = False


def _decode(jpeg_bytes: bytes) -> Optional[np.ndarray]:
    # pylint: disable-next=import-outside-toplevel
    import cv2

    return cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)


def find_fish_in_jpeg(
    jpeg_bytes: bytes,
    laser_points: Sequence[Sequence[float]],
    segmenter: Any,
    options: Optional[PredictOptions] = None,
    mask_sink: Any = None,
    score_gate: Optional[float] = None,
) -> Optional[FishAtDot]:
    """Core's `find_fish_at_dot` on the decoded JPEG; None when it will not
    decode. `mask_sink` (`masks.MaskSink`), when given, is handed the mask
    kept, to be stored (contract 12, 2026-10-21). `score_gate` is the
    automatic chain's (`automatic_results.frames`), and needs a scored
    segmenter."""
    options = options or PredictOptions()
    frame = _decode(jpeg_bytes)
    if frame is None:
        return None
    found = find_fish_at_dot(
        frame,
        laser_points,
        segmenter,
        crop_w=options.crop_w,
        crop_h=options.crop_h,
        grow_cut_masks=options.grow_cut_masks,
        score_gate=score_gate,
    )
    if mask_sink is not None and found.mask is not None:
        mask_sink.keep(
            found.mask, found.crop_x, found.crop_y, found.width, found.height
        )
    return found


def predict_from_jpeg(
    jpeg_bytes: bytes,
    laser_points: Sequence[Sequence[float]],
    segmenter: Any,
    capture_id: uuid.UUID,
    laser_label_ids: Optional[Sequence[uuid.UUID]] = None,
    options: Optional[PredictOptions] = None,
    mask_sink: Any = None,
) -> HeadtailPredictionResult:
    """Decode, then core's crop, segment, gate, keypoint and lift, as the
    stage's result row. `segmenter` is anything with
    `segment(image) -> list[np.ndarray]`: core's `Sam3Segmenter` or
    `FishialSegmenter` in production, a stub in tests. `laser_label_ids`, when
    given, names one label per laser point: the result names the label of the
    dot core reports (`FishAtDot.dot`)."""
    if laser_label_ids and len(laser_label_ids) != len(laser_points):
        # A caller's bug: every retry gets the same payload (review of #349).
        raise ApplicationError(
            f"{len(laser_label_ids)} laser label ids for {len(laser_points)} "
            "laser points: one per dot, or none",
            type="LaserLabelsMismatch",
            non_retryable=True,
        )
    options = options or PredictOptions()
    found = find_fish_in_jpeg(jpeg_bytes, laser_points, segmenter, options, mask_sink)
    return _result(found, capture_id, laser_label_ids, options)


def _result(
    found: Optional[FishAtDot],
    capture_id: uuid.UUID,
    laser_label_ids: Optional[Sequence[uuid.UUID]],
    options: PredictOptions,
) -> HeadtailPredictionResult:
    """Core's answer as the stage's row. With no dots there is no crop to
    place (core says (0, 0)), so the row leaves it NULL, as before."""
    provenance = {
        "capture_id": capture_id,
        "predictor_version": options.predictor_version,
        "checkpoint": options.checkpoint,
        "core_version": options.core_version,
    }
    if found is None:
        return HeadtailPredictionResult(status="decode_failed", **provenance)
    placed = found.dot is not None
    head = found.head or (None, None)
    tail = found.tail or (None, None)
    return HeadtailPredictionResult(
        status=found.status,
        width=found.width,
        height=found.height,
        crop_x=found.crop_x if placed else None,
        crop_y=found.crop_y if placed else None,
        laser_label_id=(
            laser_label_ids[found.dot] if placed and laser_label_ids else None
        ),
        mask_bbox=list(found.mask_bbox) if found.mask_bbox is not None else None,
        mask_area_px=found.mask_area_px,
        silhouette_ratio=found.silhouette_ratio,
        head_x=head[0],
        head_y=head[1],
        tail_x=tail[0],
        tail_y=tail[1],
        **provenance,
    )
