"""Stage 5.1: rectify a raw frame and write its head/tail JPEG.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
preprocess_headtail_image.py. Behaviour is v1's: download the staged raw
frame, `RectifiedImage(RawImage(raw_bytes), intrinsics)`, encode with
`cv2.imencode(".jpg")` at the default quality, **no overlay** (head/tail
labeling wants the bare rectified frame), upload. The output must stay
byte-identical to the stage-5.1 notebook (tests/test_headtail_preprocess.py).

v2 changes:

* the activity is handed ObjectRefs -- where the raw frame was staged and where
  the JPEG goes (over v1's JPEG for a migrated frame, so its Label Studio
  tasks keep their URL) -- instead of a checksum and a folder;
* the intrinsics are fishsense-core's own `CameraIntrinsics` (v1 passed the API
  SDK's, which core no longer needs);
* the object store is built on first use, from `FISHSENSE_OBJECT_STORE_*`, and
  reused; v1 opened a client per activity.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from temporalio import activity

from fishsense_services_contracts.headtail import PreprocessHeadtailImageInput
from fishsense_services_processor.jpeg import encode_jpeg as encode_rectified_jpeg

__all__ = [
    "HeadtailPreprocessActivities",
    "encode_rectified_jpeg",
    "rectify_and_encode_jpeg",
]


def rectify_and_encode_jpeg(
    raw_bytes: bytes,
    camera_matrix: list[list[float]],
    distortion_coefficients: list[float],
) -> bytes:
    """Decode, rectify and encode one frame. Synchronous: run it in a thread."""
    # pylint: disable=import-outside-toplevel
    from fishsense_core.camera_intrinsics import CameraIntrinsics
    from fishsense_core.image.raw_image import RawImage
    from fishsense_core.image.rectified_image import RectifiedImage

    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion_coefficients=np.array(distortion_coefficients, dtype=float),
    )
    return encode_rectified_jpeg(RectifiedImage(RawImage(raw_bytes), intrinsics).data)


class HeadtailPreprocessActivities:  # pylint: disable=too-few-public-methods
    """Stage 5.1's activity, given how to reach the object store."""

    def __init__(self, *, store_factory: Callable[[], Any]) -> None:
        self._store_factory = store_factory
        self._store: Any = None

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="preprocess_headtail_image")
    async def preprocess_headtail_image(
        self, payload: PreprocessHeadtailImageInput
    ) -> None:
        """Download one staged raw frame, rectify it, and write the JPEG where
        the orchestrator said."""
        activity.logger.info("preprocessing headtail image raw=%s", payload.raw.uri)
        store = self._object_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = await store.download_raw(payload.raw, Path(tmpdir))
            raw_bytes = await asyncio.to_thread(path.read_bytes)
        jpeg = await asyncio.to_thread(
            rectify_and_encode_jpeg,
            raw_bytes,
            payload.camera_matrix,
            payload.distortion_coefficients,
        )
        await store.upload_processed_jpeg(payload.jpeg, jpeg)
