"""The shadow calibration's processor activities.

- `segment_slate_frame` (GPU role): one slate frame's render, read by its
  presigned GET, downscaled and segmented as the port did (`segment`). A
  render that isn't there or won't decode is that frame's status, not a
  failure: the fit counts it as a frame without a mask.
- `fit_shadow_calibration` (light role): both dot sources, both variants
  (`run`), heartbeating while it computes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.shadow_calibration import (
    FitShadowCalibrationInput,
    SegmentSlateFrame,
    ShadowFitResult,
    SlateSegment,
)
from fishsense_services_processor.shadow_calibration.run import run_shadow_fit
from fishsense_services_processor.shadow_calibration.segment import (
    downscaled,
    segment_dots,
)

__all__ = ["HEARTBEAT_EVERY_S", "ShadowCalibrationActivities"]

HEARTBEAT_EVERY_S = 30.0


def _no_gpu() -> ApplicationError:
    return ApplicationError(
        "SAM 3.1 needs a CUDA GPU", type="NoGpuForSam3", non_retryable=True
    )


async def _while_heartbeating(fn, *args):
    """`fn(*args)` in a thread, heartbeating until it returns."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    while True:
        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S)
        if done:
            return task.result()
        activity.heartbeat()


class ShadowCalibrationActivities:
    """`segmenter(path)` gives ``(image, prompt) -> [(mask, score)]`` at the
    port's threshold; `sam3_checkpoint()` the verified checkpoint and its id."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        sam3_checkpoint: Callable[[], Awaitable[tuple[Path, str]]],
        cuda_available: Callable[[], bool],
        segmenter: Callable[[str], Any],
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._sam3_checkpoint = sam3_checkpoint
        self._cuda_available = cuda_available
        self._segmenter = segmenter

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="segment_slate_frame")
    async def segment_slate_frame(self, frame: SegmentSlateFrame) -> SlateSegment:
        if not self._cuda_available():
            raise _no_gpu()
        try:
            jpeg = await self._object_store().download_image(url=frame.render_url)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            activity.logger.warning("capture=%s: render unavailable: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return SlateSegment(
                capture_id=frame.capture_id, status="render_unavailable"
            )
        try:
            image, scale = await asyncio.to_thread(downscaled, jpeg)
        except (OSError, ValueError) as exc:
            activity.logger.warning("capture=%s: render would not decode: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return SlateSegment(capture_id=frame.capture_id, status="decode_failed")
        path, model_id = await self._sam3_checkpoint()
        segmenter = await asyncio.to_thread(self._segmenter, str(path))
        dots = await asyncio.to_thread(
            segment_dots, image, scale, frame.dots, segmenter
        )
        return SlateSegment(capture_id=frame.capture_id, status="segmented",
                            render_scale=scale, sam_model=model_id, dots=dots)  # fmt: skip

    @activity.defn(name="fit_shadow_calibration")
    async def fit_shadow_calibration(
        self, payload: FitShadowCalibrationInput
    ) -> ShadowFitResult:
        result = await _while_heartbeating(run_shadow_fit, payload)
        activity.logger.info(
            "shadow calibration mission=%s: %s", payload.mission_id,
            ", ".join(f"{c.dot_source}/{c.variant}={c.outcome}"
                      f"{'' if c.refusal_reason is None else ':' + c.refusal_reason}"
                      for c in result.calibrations),
        )  # fmt: skip
        return result
