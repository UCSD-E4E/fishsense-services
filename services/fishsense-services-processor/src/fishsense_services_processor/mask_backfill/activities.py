"""The mask backfill's GPU activity: one prediction's mask, regenerated,
checked against the prediction, and PUT only when it reproduces it.

The image is the prediction's own: the automatic track's render by the GET
the orchestrator presigned, or the head/tail stage's stage-5.1 JPEG by
reference. One that is gone, or will not decode, is said, not raised: the
prediction keeps no mask, and nothing retries for it.
"""

from __future__ import annotations

import asyncio
import urllib.error
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import ClientError
from temporalio import activity

from fishsense_services_contracts.mask_backfill import (
    BackfilledMask,
    BackfillMaskFrame,
)
from fishsense_services_processor.headtail_predict.predict import _no_gpu
from fishsense_services_processor.mask_backfill.regenerate import regenerate
from fishsense_services_processor.masks import Put, store_mask_or_none
from fishsense_services_processor.renders.activities import http_put

__all__ = ["MaskBackfillActivities"]

_MISSING = {"404", "NoSuchKey", "NotFound"}


class MaskBackfillActivities:  # pylint: disable=too-few-public-methods
    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        store_factory: Callable[[], Any],
        sam3_checkpoint: Callable[[], Awaitable[tuple[Path, str]]],
        cuda_available: Callable[[], bool],
        segmenter: Callable[[str], Any],
        put: Put = http_put,
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._sam3_checkpoint = sam3_checkpoint
        self._cuda_available = cuda_available
        self._segmenter = segmenter
        self._put = put

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    async def _image(self, frame: BackfillMaskFrame) -> Optional[bytes]:
        store = self._object_store()
        try:
            if frame.render_url is not None:
                return await store.download_image(url=frame.render_url)
            return await store.download_processed_jpeg(frame.jpeg)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise

    @activity.defn(name="backfill_prediction_mask")
    async def backfill_prediction_mask(
        self, source: str, frame: BackfillMaskFrame
    ) -> BackfilledMask:
        if not self._cuda_available():
            raise _no_gpu()
        image = await self._image(frame)
        if image is None:
            activity.logger.warning("capture=%s: image gone", frame.capture_id)
            return BackfilledMask(prediction_id=frame.prediction_id,
                                  capture_id=frame.capture_id,
                                  outcome="image_unavailable")  # fmt: skip
        path, model_id = await self._sam3_checkpoint()
        segmenter = await asyncio.to_thread(self._segmenter, str(path))
        outcome, got, mask = await asyncio.to_thread(
            lambda: regenerate(source, frame, image, segmenter=segmenter,
                               checkpoint=model_id,
                               core_version=version("fishsense-core"))  # fmt: skip
        )
        stored = None
        if mask is not None and frame.mask is not None:
            stored = await asyncio.to_thread(
                lambda: store_mask_or_none(
                    self._put, frame.mask, mask, capture_id=frame.capture_id
                )
            )
        activity.logger.info(
            "mask backfill %s capture=%s prediction=%s: %s%s", source,
            frame.capture_id, frame.prediction_id, outcome,
            "" if got is None or outcome == "reproduced"
            else f" (got area {got.mask_area_px} crop {got.crop_x},{got.crop_y};"
                 f" recorded {frame.expect.mask_area_px} "
                 f"{frame.expect.crop_x},{frame.expect.crop_y})",
        )  # fmt: skip
        return BackfilledMask(
            prediction_id=frame.prediction_id, capture_id=frame.capture_id,
            outcome=outcome, got=got, mask=stored,
        )  # fmt: skip
