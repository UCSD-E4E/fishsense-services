"""The laser-colour backfill's activity: `read_laser_colour`, one dot.

The render the dot's prediction was made on is read by presigned GET and
decoded, and fishsense-core's `classify_laser_color` -- the live laser
kernel's rule (`laser_predict.activities._predict_from_raw`) -- reads the
colour at the dot. The dot is in the render's pixels, so no mapping. The
input differs from the kernel's: an 8-bit render, not the linear raw; the run
reports its agreement before anything is written (contract v24). A render not
there or not decoding is the frame's status, not a retry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio import activity

from fishsense_services_contracts.laser_colour_backfill import (
    ColourFrame,
    ColourReading,
)

__all__ = ["LaserColourActivities", "read_colour"]


def read_colour(jpeg: bytes, x: float, y: float) -> tuple[str | None, float | None]:
    """``(colour, margin)`` at ``(x, y)`` of the JPEG. Blocking. Raises
    `ValueError` if it does not decode."""
    # pylint: disable=import-outside-toplevel
    import cv2
    import numpy as np
    from fishsense_core.laser_color import classify_laser_color

    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("the render does not decode")
    colour, margin = classify_laser_color(bgr, x, y)
    return colour, None if margin is None else float(margin)


class LaserColourActivities:  # pylint: disable=too-few-public-methods
    def __init__(self, *, store_factory: Callable[[], Any]) -> None:
        self._store_factory = store_factory
        self._store: Any = None

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="read_laser_colour")
    async def read_laser_colour(self, frame: ColourFrame) -> ColourReading:
        ids = {"prediction_id": frame.prediction_id, "capture_id": frame.capture_id}
        try:
            jpeg = await self._object_store().download_image(url=frame.render_url)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            activity.logger.warning("capture=%s: render unavailable: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return ColourReading(**ids, status="render_unavailable")
        try:
            colour, margin = await asyncio.to_thread(
                read_colour, jpeg, frame.x, frame.y
            )
        except ValueError as exc:
            activity.logger.warning("capture=%s: render would not decode: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return ColourReading(**ids, status="decode_failed")
        return ColourReading(**ids, status="read", color=colour, margin=margin)
