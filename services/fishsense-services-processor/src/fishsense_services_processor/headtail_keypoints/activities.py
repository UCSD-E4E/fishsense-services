"""The keypoint replay's GPU activity: `predict_headtail_keypoints`, one frame
(`predict.keypoints_frame`), its render read by presigned GET. A render not
there or not decoding is the frame's status; a worker without CUDA refuses
(the model was trained and evaluated under CUDA bf16 autocast, which core
uses only on CUDA). Weights that cannot load are the worker's too, a
non-retryable ``HeadtailKeypoints{kind}`` (2026-10-24: before, each frame
retried the load three times and was recorded ``failed``)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.headtail_keypoints import (
    KeypointFrame,
    KeypointResult,
)
from fishsense_services_contracts.refusals import HEADTAIL_KEYPOINTS
from fishsense_services_processor.headtail_keypoints.predict import keypoints_frame
from fishsense_services_processor.weights import refuse_unloadable_weights

__all__ = ["HeadtailKeypointActivities"]


class HeadtailKeypointActivities:  # pylint: disable=too-few-public-methods
    """`detector()` gives the loaded, verified model (shared, loaded once)."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        detector: Callable[[], Any],
        checkpoint: str,
        cuda_available: Callable[[], bool],
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._detector = detector
        self._checkpoint = checkpoint
        self._cuda_available = cuda_available

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="predict_headtail_keypoints")
    async def predict_headtail_keypoints(self, frame: KeypointFrame) -> KeypointResult:
        if not self._cuda_available():
            raise ApplicationError("the keypoint model runs on a CUDA GPU",
                                   type="NoGpuForKeypoints", non_retryable=True)  # fmt: skip
        ids = {"prediction_id": frame.prediction_id, "capture_id": frame.capture_id}
        try:
            jpeg = await self._object_store().download_image(url=frame.render_url)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            activity.logger.warning("capture=%s: render unavailable: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return KeypointResult(**ids, status="render_unavailable")
        with refuse_unloadable_weights(HEADTAIL_KEYPOINTS, "the keypoint model"):
            detector = await asyncio.to_thread(self._detector)
        try:
            found = await asyncio.to_thread(
                keypoints_frame, jpeg, frame.mask_bbox, detector
            )
        except ValueError as exc:
            activity.logger.warning("capture=%s: render would not decode: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return KeypointResult(**ids, status="decode_failed")
        return KeypointResult(**ids, checkpoint=self._checkpoint, **found)
