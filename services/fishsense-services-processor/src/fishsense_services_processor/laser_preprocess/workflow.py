"""Stage 0.1 workflow: fan `preprocess_laser_image` out across the dive's
images that need a laser JPEG.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
preprocess_laser_images_workflow.py. Inputs are pre-resolved by the
orchestrator's `PreprocessLaserImagesParentWorkflow`, which selects the dive,
reads its captures and intrinsics, stages the raw frames and then dispatches
this; it touches no database, NAS or key layout itself.

v2 change: each image carries the `ObjectRef` of its staged raw frame and of
where its JPEG goes (v1: a checksum and a fixed folder).
"""

import asyncio
from datetime import timedelta
from typing import List, Optional, Tuple
from uuid import UUID

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import
    from pydantic import BaseModel

    from fishsense_services_contracts.laser import PreprocessLaserImagesInput
    from fishsense_services_contracts.object_store import ObjectRef

__all__ = ["PreprocessLaserImageInput", "PreprocessLaserImagesWorkflow"]


class PreprocessLaserImageInput(BaseModel):
    """Per-image payload for the `preprocess_laser_image` activity.

    `region` is the shape drawn; `bbox` its bounding box and the fallback when
    the parent's payload predates the polygon (v1's rolling-deploy reason).
    """

    capture_id: UUID
    raw: ObjectRef
    jpeg: ObjectRef
    bbox: Tuple[int, int, int, int]  # (x1, y1, x2, y2), rectified pixels
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    region: Optional[List[List[int]]] = None


@workflow.defn
class PreprocessLaserImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessLaserImagesInput) -> None:
        workflow.logger.info(
            "preprocessing laser images dive_id=%s images=%d",
            payload.dive_id,
            len(payload.images),
        )
        await asyncio.gather(
            *[
                workflow.execute_activity(
                    "preprocess_laser_image",
                    PreprocessLaserImageInput(
                        capture_id=image.capture_id,
                        raw=image.raw,
                        jpeg=image.jpeg,
                        bbox=tuple(payload.bbox),
                        region=payload.laser_region,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                    ),
                    # start_to_close, not schedule_to_close: a dive's images
                    # queue behind the per-image cap of 2 (v1).
                    start_to_close_timeout=timedelta(minutes=5),
                )
                for image in payload.images
            ]
        )
