"""The head/tail predict activity: pick the backend, read the JPEG, predict.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
predict_headtail_image.py (`predict_headtail_image`). Behavior is v1's:

* **a GPU-less worker leaves an existing row alone** unless the laser behind
  it was superseded -- rewriting would be identical (a fallback row) or a
  downgrade (a SAM 3.1 row) -- and says so with `skipped_no_upgrade_available`,
  which the parent drops;
* with a GPU, SAM 3.1 (`HEADTAIL_PREDICTOR_VERSION`); without, fishsense-core's
  Mask R-CNN (`HEADTAIL_FALLBACK_PREDICTOR_VERSION`, permanently stale, so the
  row is upgraded when a GPU returns);
* the weights load and the inference run off the event loop.

v2 changes: the JPEG is read from the ref the orchestrator hands over; the
SAM 3.1 checkpoint is fetched and verified through fishsense-core
(`headtail_predict.weights`), and a row records that model's id rather than
v1's pod-local cache path; `core_version` is recorded (v1 never set it); a
weights failure (settings, integrity, a missing object, no manifest entry) is
non-retryable, each its own type, where v1's retried the download forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from fishsense_core.fish.fish_at_dot import FishialSegmenter, Sam3Segmenter
from temporalio import activity

from fishsense_services_contracts.headtail import (
    HEADTAIL_FALLBACK_PREDICTOR_VERSION,
    HEADTAIL_PREDICTOR_VERSION,
    HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
    HeadtailPredictionResult,
    PredictHeadtailImage,
)
from fishsense_services_contracts.masks import KEPT_MASK_STATUSES
from fishsense_services_contracts.refusals import SAM3
from fishsense_services_processor.masks import MaskSink, Put, store_mask_or_none
from fishsense_services_processor.renders.activities import http_put
from fishsense_services_processor.weights import refuse_unloadable_weights
from fishsense_services_processor.headtail_predict.predict import (
    FALLBACK_CHECKPOINT,
    PredictOptions,
    cuda_available,
    get_fallback_segmenter,
    get_segmenter,
    predict_from_jpeg,
)

__all__ = ["HeadtailPredictActivities"]


def _core_version() -> str:
    return version("fishsense-core")


class HeadtailPredictActivities:  # pylint: disable=too-few-public-methods
    """The predict activity, given how to reach the object store and how to
    get SAM 3.1's verified checkpoint (``(path, model id)``)."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        sam3_checkpoint: Callable[[], Awaitable[tuple[Path, str]]],
        put: Put = http_put,
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._sam3_checkpoint = sam3_checkpoint
        #: PUTs a kept mask to its presigned key (contract 12).
        self._put = put

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    async def _verified_sam3_checkpoint(self) -> tuple[Path, str]:
        """SAM 3.1's checkpoint, or a non-retryable failure naming why not
        (``Sam3SettingsInvalid``, ``Sam3WeightsCorrupt``,
        ``Sam3WeightsUnavailable``, ``Sam3NotInManifest``; the shared
        `weights.refuse_unloadable_weights` since 2026-10-24).

        None of these is the image's, and none passes on a retry: left plain,
        every image of the dive retried (without limit until 2026-10-24, when
        the workflow had no retry policy), each attempt re-downloading and
        re-hashing gigabytes, since core caches only a verified file. The
        automatic frames, mask backfill, slate pose and shadow stages reuse
        this method for their SAM 3.1."""
        with refuse_unloadable_weights(SAM3, "SAM 3.1"):
            return await self._sam3_checkpoint()

    @activity.defn(name="predict_headtail_image")
    async def predict_headtail_image(
        self, payload: PredictHeadtailImage
    ) -> HeadtailPredictionResult:
        """Predict one image's snout and fork from its stage-5.1 JPEG."""
        on_gpu = cuda_available()
        if (
            not on_gpu
            and payload.has_existing_prediction
            and not payload.existing_laser_superseded
        ):
            activity.logger.info(
                "skipping capture=%s: already predicted and no GPU to improve on it",
                payload.capture_id,
            )
            return HeadtailPredictionResult(
                capture_id=payload.capture_id,
                status=HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
                predictor_version=HEADTAIL_FALLBACK_PREDICTOR_VERSION,
            )

        if on_gpu:
            path, model_id = await self._verified_sam3_checkpoint()
            segmenter: Any = Sam3Segmenter(
                await asyncio.to_thread(get_segmenter, str(path))
            )
            options = PredictOptions(
                checkpoint=model_id,
                core_version=_core_version(),
                predictor_version=HEADTAIL_PREDICTOR_VERSION,
            )
        else:
            segmenter = FishialSegmenter(
                await asyncio.to_thread(get_fallback_segmenter)
            )
            options = PredictOptions(
                checkpoint=FALLBACK_CHECKPOINT,
                core_version=_core_version(),
                predictor_version=HEADTAIL_FALLBACK_PREDICTOR_VERSION,
            )

        jpeg = await self._object_store().download_processed_jpeg(payload.jpeg)
        # Contract 12: an image with an upload slot keeps its mask, to store.
        sink = MaskSink() if payload.mask is not None else None
        result = await asyncio.to_thread(
            predict_from_jpeg,
            jpeg,
            payload.laser_points,
            segmenter,
            payload.capture_id,
            payload.laser_label_ids,
            options,
            sink,
        )
        if (
            sink is not None
            and sink.mask is not None
            and (result.status in KEPT_MASK_STATUSES)
        ):
            stored = await asyncio.to_thread(
                lambda: store_mask_or_none(
                    self._put, payload.mask, sink.mask, capture_id=payload.capture_id
                )
            )
            if stored is not None:
                result = result.model_copy(update={"mask": stored})
        activity.logger.info(
            "predicted headtail capture=%s status=%s crop=(%s,%s) ratio=%s",
            result.capture_id,
            result.status,
            result.crop_x,
            result.crop_y,
            (
                None
                if result.silhouette_ratio is None
                else round(result.silhouette_ratio, 3)
            ),
        )
        return result
