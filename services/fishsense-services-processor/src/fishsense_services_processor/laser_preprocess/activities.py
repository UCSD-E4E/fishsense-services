"""Stage 0.1: rectify a raw laser frame, draw the expected-laser region, and
write the JPEG to the object store.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
preprocess_laser_image.py (the activity; the pure core is `overlay`).

v2 changes: the raw frame and the JPEG are `ObjectRef`s the orchestrator
issued -- for a migrated frame the JPEG ref is v1's key, so a redraw overwrites
in place and Label Studio's task URL never moves -- and the processor's store
refuses to write anything but a processed JPEG. The raw frame is streamed to a
temporary file rather than held in memory.
"""

from __future__ import annotations

import asyncio
import tempfile
from functools import cache
from pathlib import Path

from temporalio import activity

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.laser_preprocess.overlay import (
    rectify_overlay_encode,
)
from fishsense_services_processor.laser_preprocess.workflow import (
    PreprocessLaserImageInput,
)
from fishsense_services_processor.object_store import ProcessorObjectStore

__all__ = ["preprocess_laser_image"]

#: Seam for the tests; the real transform.
_rectify_overlay_encode = rectify_overlay_encode


@cache
def _object_store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


@activity.defn(name="preprocess_laser_image")
async def preprocess_laser_image(payload: PreprocessLaserImageInput) -> None:
    """Stream the staged raw frame, rectify it, draw the region, write the JPEG."""
    if not isinstance(payload, PreprocessLaserImageInput):
        payload = PreprocessLaserImageInput.model_validate(payload)
    region = payload.region
    activity.logger.info(
        "preprocessing laser image capture_id=%s raw=%s shape=%s",
        payload.capture_id,
        payload.raw.uri,
        "polygon" if region else f"bbox {payload.bbox}",
    )
    store = _object_store()
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = await store.download_raw(payload.raw, Path(tmpdir))
        jpeg = await asyncio.to_thread(
            _rectify_overlay_encode,
            raw_path,
            payload.camera_matrix,
            payload.distortion_coefficients,
            tuple(payload.bbox),
            region,
        )
    await store.upload_processed_jpeg(payload.jpeg, jpeg)
