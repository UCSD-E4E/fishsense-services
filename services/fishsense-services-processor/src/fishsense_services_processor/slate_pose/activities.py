"""Calibration v4's GPU activity: `slate_pose_frame`, one slate frame's pose
(`frame.pose_frame`), its render read by its presigned GET and the template's
PDF from scratch once per worker. A render not there or not decoding is the
frame's status; a worker without CUDA refuses (SAM 3.1), as the shadow
calibration's segmentation does."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
from temporalio import activity

from fishsense_services_contracts.calibration_v4 import SlatePose, SlatePoseFramesInput
from fishsense_services_processor.shadow_calibration.activities import (
    _no_gpu,
    _while_heartbeating,
)
from fishsense_services_processor.slate_pose.frame import pose_frame, render_page

__all__ = ["SlatePoseActivities"]


class SlatePoseActivities:  # pylint: disable=too-few-public-methods
    """`segmenter(path)` gives ``(image, prompt) -> [(mask, score)]`` at the
    shadow calibration's threshold; `sam3_checkpoint()` the verified
    checkpoint and its id."""

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
        self._pages: dict[tuple, np.ndarray] = {}

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    async def _page(self, request: SlatePoseFramesInput) -> np.ndarray:
        key = (request.slate_pdf.bucket, request.slate_pdf.key, request.slate_dpi)
        if key not in self._pages:
            pdf = await self._object_store().download_slate_pdf(request.slate_pdf)
            self._pages[key] = await asyncio.to_thread(
                render_page, pdf, request.slate_dpi
            )
        return self._pages[key]

    @activity.defn(name="slate_pose_frame")
    async def slate_pose_frame(self, request: SlatePoseFramesInput) -> SlatePose:
        """The pose of the request's one frame."""
        if not self._cuda_available():
            raise _no_gpu()
        (frame,) = request.frames
        try:
            jpeg = await self._object_store().download_image(url=frame.render_url)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            activity.logger.warning("capture=%s: render unavailable: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return SlatePose(capture_id=frame.capture_id, status="render_unavailable")
        page = await self._page(request)
        path, _ = await self._sam3_checkpoint()
        segmenter = await asyncio.to_thread(self._segmenter, str(path))
        try:
            found = await _while_heartbeating(
                lambda: pose_frame(jpeg, frame.dot, request.camera_matrix,
                                   segmenter=segmenter, page_grey=page,
                                   dpi=request.slate_dpi,
                                   reference_points=request.reference_points)  # fmt: skip
            )
        except (OSError, ValueError) as exc:
            activity.logger.warning("capture=%s: render would not decode: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return SlatePose(capture_id=frame.capture_id, status="decode_failed")
        return SlatePose(capture_id=frame.capture_id, **found)
