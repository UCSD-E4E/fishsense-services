"""Stage 2: rectify a raw species frame, draw its place in its cluster, and
write the JPEG a labeler sees.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
preprocess_species_image.py. Behaviour is v1's, and the overlay is the
notebook's, byte for byte: "{i}/{N}" at (w-350, h-75), FONT_HERSHEY_SIMPLEX
scale 5, red (BGR 0,0,255), thickness 10, LINE_AA, `cv2.imencode` at its
default quality. The decode is fishsense-core's `RectifiedImage(RawImage)` at
its default configuration, as v1 runs it on core 4.1.0.

v2 changes:

* it reads and writes the `ObjectRef`s in its payload -- the orchestrator
  issued them -- and builds no key. A migrated frame's JPEG is v1's, written
  over in place, as v1 did;
* rectification is handed core's own `CameraIntrinsics` (v1 built the API
  SDK's, which v2 does not have);
* the raw frame is streamed to a scratch file (`ProcessorObjectStore.
  download_raw`) instead of held in memory, and removed afterwards.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from fishsense_core.camera_intrinsics import CameraIntrinsics
from fishsense_core.image.raw_image import RawImage
from fishsense_core.image.rectified_image import RectifiedImage
from temporalio import activity

from fishsense_services_processor.jpeg import encode_jpeg
from fishsense_services_processor.species.workflow import PreprocessSpeciesImageInput

__all__ = ["SpeciesImageActivities", "overlay_and_encode_jpeg"]


def overlay_and_encode_jpeg(
    rectified_bgr: np.ndarray,
    cluster_index: int,
    cluster_size: int,
) -> bytes:
    """Draw the 1-based cluster index in the bottom-right corner and
    encode to JPEG bytes. Does not mutate the input."""
    img = rectified_bgr.copy()
    height, width = img.shape[:2]
    cv2.putText(
        img,
        f"{cluster_index}/{cluster_size}",
        (width - 350, height - 75),
        cv2.FONT_HERSHEY_SIMPLEX,
        5,
        (0, 0, 255),
        10,
        cv2.LINE_AA,
    )
    return encode_jpeg(img)


def _rectify_overlay_encode(
    raw: Path,
    camera_matrix: list[list[float]],
    distortion_coefficients: list[float],
    cluster_index: int,
    cluster_size: int,
) -> bytes:
    """Run via asyncio.to_thread -- heavy CPU work (rawpy decode, undistort)."""
    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion_coefficients=np.array(distortion_coefficients, dtype=float),
    )
    rectified = RectifiedImage(RawImage(raw), intrinsics)
    return overlay_and_encode_jpeg(rectified.data, cluster_index, cluster_size)


class SpeciesImageActivities:
    """The stage's activity, given how to reach the object store. The store is
    built on first use: the stage is declared at import, before a worker has
    its settings."""

    def __init__(self, *, store_factory: Callable[[], object]) -> None:
        self._store_factory = store_factory
        self._store = None

    def _object_store(self):
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="preprocess_species_image")
    async def preprocess_species_image(
        self, payload: PreprocessSpeciesImageInput
    ) -> None:
        """Download one staged raw frame, rectify it, draw "i/N", and write
        the JPEG over whatever is at its key."""
        member = payload.member
        activity.logger.info(
            "preprocessing species image capture=%s cluster=%d/%d",
            member.capture_id,
            member.cluster_index,
            member.cluster_size,
        )
        store = self._object_store()
        with tempfile.TemporaryDirectory(prefix="species-") as scratch:
            raw = await store.download_raw(member.raw, Path(scratch))
            jpeg = await asyncio.to_thread(
                _rectify_overlay_encode,
                raw,
                payload.camera_matrix,
                payload.distortion_coefficients,
                member.cluster_index,
                member.cluster_size,
            )
        await store.upload_processed_jpeg(member.jpeg, jpeg)
