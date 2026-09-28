"""Model-assisted laser labeling: fan `predict_laser_image` out across a
dive's images and return one prediction per image.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
predict_laser_images_workflow.py. The fishsense-core `LaserDetector` runs on
the GPU role (served by the GPU Deployment or its CPU fallback). Same split as
v1: the orchestrator parent selects, resolves, stages the raw frames and
dispatches this; the per-image input stays here because it is only built
inside the fan-out.

v2 change: each image is a capture id and the `ObjectRef` of its staged raw
frame; the processor never builds a key.
"""

import asyncio
from datetime import timedelta
from typing import List
from uuid import UUID

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import
    from pydantic import BaseModel

    from fishsense_services_contracts.laser import (
        LaserPredictionResult,
        PredictLaserImagesInput,
    )
    from fishsense_services_contracts.object_store import ObjectRef

__all__ = ["PredictLaserImageInput", "PredictLaserImagesWorkflow"]


class PredictLaserImageInput(BaseModel):
    """Per-image payload for the `predict_laser_image` activity."""

    capture_id: UUID
    raw: ObjectRef
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    # "red" / "green", or None: the model's unknown-wavelength channel.
    wavelength: str | None = None
    # Convex polygon of rectified [x, y]; a dot outside it is not believed.
    # None disables the gate (an orchestrator that predates it sends None).
    laser_region: List[List[int]] | None = None


@workflow.defn
class PredictLaserImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictLaserImagesInput
    ) -> List[LaserPredictionResult]:
        workflow.logger.info(
            "predicting laser images dive_id=%s images=%d",
            payload.dive_id,
            len(payload.images),
        )
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_laser_image",
                    PredictLaserImageInput(
                        capture_id=image.capture_id,
                        raw=image.raw,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                        wavelength=payload.wavelength,
                        laser_region=payload.laser_region,
                    ),
                    # start_to_close, not schedule_to_close: a dive's images
                    # queue behind the role's cap of 2 (v1's fan-out rule).
                    start_to_close_timeout=timedelta(minutes=10),
                    result_type=LaserPredictionResult,
                )
                for image in payload.images
            ]
        )
        return list(results)
