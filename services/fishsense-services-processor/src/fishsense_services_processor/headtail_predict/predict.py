"""Head/tail prediction: a SAM 3.1 mask of the lasered fish, keypointed.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
predict_headtail_image.py (everything but the activity, which is in
`activities`). Behaviour is v1's, and three of its choices are measured:

* **read the stage-5.1 JPEG**, the exact frame the labeler is shown;
* **crop, don't tile**: one 1800x1350 window centred on the *first* laser dot,
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
`headtail_failed`, else the crop's centre, the first), where v1 left it NULL
and a corrected dot never made the row stale.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

import numpy as np
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.headtail import (
    HEADTAIL_CROP_HEIGHT,
    HEADTAIL_CROP_WIDTH,
    HEADTAIL_PREDICTOR_VERSION,
    HeadtailPredictionResult,
)
from fishsense_services_processor.headtail_predict.geometry import (
    crop_origin,
    lift_point,
    mask_at_point,
    silhouette_ratio,
)

__all__ = [
    "FALLBACK_CHECKPOINT",
    "PredictOptions",
    "cuda_available",
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


def _laser_label_for_mask(
    local_points: Sequence[Sequence[float]],
    laser_label_ids: Optional[Sequence[uuid.UUID]],
    binary,
) -> Optional[uuid.UUID]:
    """Which laser label landed on the chosen mask, if any were supplied."""
    if not laser_label_ids:
        return None
    for (px, py), label_id in zip(local_points, laser_label_ids):
        xi, yi = int(round(px)), int(round(py))
        if 0 <= yi < binary.shape[0] and 0 <= xi < binary.shape[1] and binary[yi, xi]:
            return label_id
    return None


def predict_from_jpeg(
    jpeg_bytes: bytes,
    laser_points: Sequence[Sequence[float]],
    segmenter: Any,
    capture_id: uuid.UUID,
    laser_label_ids: Optional[Sequence[uuid.UUID]] = None,
    options: Optional[PredictOptions] = None,
) -> HeadtailPredictionResult:
    """Decode, crop, segment, gate, keypoint, and lift back to frame pixels.

    `segmenter` is anything with `segment(image) -> list[np.ndarray]` of
    crop-local binary masks: production passes an adapter, tests a stub.
    """
    # pylint: disable=import-outside-toplevel
    import cv2

    options = options or PredictOptions()

    def _abstain(status: str, **extra) -> HeadtailPredictionResult:
        return HeadtailPredictionResult(
            capture_id=capture_id,
            status=status,
            predictor_version=options.predictor_version,
            checkpoint=options.checkpoint,
            core_version=options.core_version,
            **extra,
        )

    frame = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return _abstain("decode_failed")
    height, width = frame.shape[:2]

    if not laser_points:
        return _abstain("laser_off_all_fish", width=width, height=height)

    origin_x, origin_y = crop_origin(
        laser_points[0][0],
        laser_points[0][1],
        width,
        height,
        options.crop_w,
        options.crop_h,
    )
    crop = np.ascontiguousarray(
        frame[
            origin_y : origin_y + options.crop_h,
            origin_x : origin_x + options.crop_w,
        ]
    )

    masks = segmenter.segment(crop)
    # v2: with no mask kept, the answer came from the crop, centred on the
    # first dot, so the abstention names it: a correction to that dot then
    # makes the row stale (v1 left it NULL, and "no fish" stood forever).
    placed = {
        "width": width,
        "height": height,
        "crop_x": origin_x,
        "crop_y": origin_y,
        "laser_label_id": laser_label_ids[0] if laser_label_ids else None,
    }
    if not masks:
        return _abstain("no_detections", **placed)

    local_points = [(px - origin_x, py - origin_y) for px, py in laser_points]
    mask = mask_at_point(masks, local_points)
    if mask is None:
        return _abstain("laser_off_all_fish", **placed)

    binary = (np.asarray(mask) > 0).astype(np.uint8)
    return _keypoint(
        binary,
        capture_id=capture_id,
        origin=(origin_x, origin_y),
        frame_size=(width, height),
        local_points=local_points,
        laser_label_ids=laser_label_ids,
        options=options,
    )


def _keypoint(
    binary,
    *,
    capture_id: uuid.UUID,
    origin: tuple,
    frame_size: tuple,
    local_points: Sequence[Sequence[float]],
    laser_label_ids: Optional[Sequence[uuid.UUID]],
    options: PredictOptions,
) -> HeadtailPredictionResult:
    """Keypoint one chosen mask and lift the result into frame coordinates."""
    # pylint: disable=import-outside-toplevel
    from fishsense_core.fish import FishHeadTailDetector

    origin_x, origin_y = origin
    area = int(np.count_nonzero(binary))
    common = {
        "capture_id": capture_id,
        "width": frame_size[0],
        "height": frame_size[1],
        "crop_x": origin_x,
        "crop_y": origin_y,
        "predictor_version": options.predictor_version,
        "checkpoint": options.checkpoint,
        "core_version": options.core_version,
        # The dot that chose the mask, on a failure too (v2; v1: NULL).
        "laser_label_id": _laser_label_for_mask(local_points, laser_label_ids, binary),
    }

    try:
        head, tail = FishHeadTailDetector().find_head_tail_img(binary * 255)
    # A native call with no documented exception hierarchy; one unfittable
    # mask must not fail the whole per-image activity.
    # pylint: disable-next=broad-exception-caught
    except Exception as exc:
        _log.warning("capture=%s find_head_tail_img failed: %s", capture_id, exc)
        return HeadtailPredictionResult(
            status="headtail_failed", mask_area_px=area, **common
        )

    head_x, head_y = lift_point(head, origin_x, origin_y)
    tail_x, tail_y = lift_point(tail, origin_x, origin_y)
    length = float(np.hypot(head_x - tail_x, head_y - tail_y))

    return HeadtailPredictionResult(
        status="predicted",
        head_x=head_x,
        head_y=head_y,
        tail_x=tail_x,
        tail_y=tail_y,
        mask_area_px=area,
        silhouette_ratio=silhouette_ratio(area, length),
        **common,
    )


def _to_numpy(mask) -> np.ndarray:
    """A mask as an ndarray, whatever the backend handed back: a device
    tensor is detached and moved to the CPU first."""
    if getattr(mask, "detach", None) is not None:
        mask = mask.detach().cpu()
    return np.asarray(mask)


class _FishialAdapter:  # pylint: disable=too-few-public-methods
    """`FishSegmentation` behind the `segment` seam: BGR in, landscape only,
    its instance label map split into binary masks."""

    def __init__(self, segmentation: Any):
        self._segmentation = segmentation

    def segment(self, image_bgr: np.ndarray) -> List[np.ndarray]:
        height, width = image_bgr.shape[:2]
        if width <= height:
            _log.warning(
                "fallback segmenter given a %dx%d (non-landscape) crop; "
                "FishSegmentation returns an empty mask for these",
                width,
                height,
            )
            return []
        labels = np.asarray(self._segmentation.inference(image_bgr))
        ids = [int(i) for i in np.unique(labels) if int(i) != 0]
        return [(labels == i) for i in ids]


class _Sam3Adapter:  # pylint: disable=too-few-public-methods
    """The SAM 3.1 processor behind the `segment` seam."""

    def __init__(self, processor: Any, prompt: str = "fish"):
        self._processor = processor
        self._prompt = prompt

    def segment(self, image_bgr: np.ndarray) -> List[np.ndarray]:
        """One concept-prompted segmentation, under bfloat16 autocast entered
        as a context manager (the thread is reused), on a PIL image (an HWC
        array yields three-pixel-wide masks)."""
        # pylint: disable=import-outside-toplevel,import-error
        import cv2
        import PIL.Image
        import torch

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        device_type = "cuda" if torch.cuda.is_available() else "cpu"
        with torch.autocast(device_type, dtype=torch.bfloat16):
            state = self._processor.set_image(PIL.Image.fromarray(rgb))
            state = self._processor.set_text_prompt(self._prompt, state)
        masks = state.get("masks") if hasattr(state, "get") else None
        if masks is None:
            return []
        return [_to_numpy(m).squeeze() for m in masks]
