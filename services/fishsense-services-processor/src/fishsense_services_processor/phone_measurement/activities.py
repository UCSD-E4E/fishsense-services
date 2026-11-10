"""The phone measurement's activity (contract 28): one phone capture measured
as FishSense Mobile measures it (`kernel`).

The capture's JPEG and depth come by the presigned GETs the orchestrator
issued (the tenant's bucket, which this processor holds no key for). A file
that is gone, or a JPEG or depth that won't read, is said, not raised: the
capture is not measured, and nothing retries for it. Any other store error
raises, to be retried. The mask, when the fish was segmented, is PUT as the
app's ``mask_png`` (`masks.store_mask_or_none`): an addition that never fails
the measurement.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.error
from collections.abc import Callable
from importlib.metadata import version
from typing import Any, Optional

from temporalio import activity

from fishsense_services_contracts.phone_measurement import (
    PHONE_ALGORITHM,
    PhoneFrame,
    PhoneMeasured,
)
from fishsense_services_processor.masks import FrameMask, Put, store_mask_or_none
from fishsense_services_processor.object_store import http_get
from fishsense_services_processor.phone_measurement.kernel import (
    decode_bgr,
    measure,
    read_depth,
)
from fishsense_services_processor.renders.activities import http_put

__all__ = ["PhoneMeasurementActivities", "fishial_segmenter"]


def fishial_segmenter() -> Any:
    """The app's segmentation, loaded: `FishSegmentation`, the ONNX `fishial`
    weights (no torch, so the CPU image runs it)."""
    # pylint: disable-next=import-outside-toplevel
    from fishsense_core.fish import FishSegmentation

    segmentation = FishSegmentation()
    segmentation.load_model()
    return segmentation


class PhoneMeasurementActivities:  # pylint: disable=too-few-public-methods
    def __init__(
        self,
        *,
        segmenter: Callable[[], Any] = fishial_segmenter,
        get: Callable[[str], bytes] = http_get,
        put: Put = http_put,
    ) -> None:
        self._segmenter_factory = segmenter
        self._segmenter: Any = None
        self._lock = threading.Lock()
        self._get = get
        self._put = put

    def _loaded(self) -> Any:
        """One model per worker, loaded once, before any thread uses it."""
        with self._lock:
            if self._segmenter is None:
                self._segmenter = self._segmenter_factory()
            return self._segmenter

    def _fetch(self, url: str) -> Optional[bytes]:
        try:
            return self._get(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _measure(self, frame: PhoneFrame) -> PhoneMeasured:
        def said(status: str, error: Optional[str] = None) -> PhoneMeasured:
            return PhoneMeasured(
                capture_id=frame.capture_id, status=status, error=error
            )

        rgb, depth = self._fetch(frame.rgb_url), self._fetch(frame.depth_url)
        if rgb is None or depth is None:
            return said("image_unavailable")
        bgr = decode_bgr(rgb)
        if bgr is None:
            return said("decode_failed", "the JPEG would not decode")
        try:
            depth_map = read_depth(depth, frame.depth_width, frame.depth_height,
                                   frame.depth_row_bytes)  # fmt: skip
        except ValueError as exc:
            return said("decode_failed", str(exc))
        r = measure(bgr, depth_map, frame.intrinsics, segmenter=self._loaded())
        stored = None
        if r.mask is not None and frame.mask is not None:
            height, width = r.mask.shape
            stored = store_mask_or_none(
                self._put, frame.mask,
                FrameMask(crop=r.mask, x=0, y=0, width=width, height=height),
                capture_id=frame.capture_id,
            )  # fmt: skip
        snout = r.snout or (None, None)
        fork = r.fork or (None, None)
        return PhoneMeasured(
            capture_id=frame.capture_id, status=r.status, length_m=r.length_m,
            snout_x=snout[0], snout_y=snout[1], fork_x=fork[0], fork_y=fork[1],
            snout_depth_m=r.snout_depth_m, fork_depth_m=r.fork_depth_m,
            model_id=r.model_id, core_version=version("fishsense-core"),
            algorithm=PHONE_ALGORITHM, mask=stored, error=r.error,
        )  # fmt: skip

    @activity.defn(name="measure_phone_capture")
    async def measure_phone_capture(self, frame: PhoneFrame) -> PhoneMeasured:
        result = await asyncio.to_thread(self._measure, frame)
        activity.logger.info("phone capture=%s: %s%s", frame.capture_id, result.status,
                             "" if result.length_m is None
                             else f" {result.length_m * 1000:.1f} mm")  # fmt: skip
        return result
