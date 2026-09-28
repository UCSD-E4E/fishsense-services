"""The head/tail predict workflow: one `predict_headtail_image` per image.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
predict_headtail_images_workflow.py. Behaviour is v1's: every image at once,
each with 15 minutes start-to-close (generous next to the ~0.6 s a prediction
takes, because a cold pod's first activity also pays the weight fetch and the
SAM 3.1 load), and one result per image, returned to the orchestrator's
parent, which persists them.

Nothing to stage: the stage-5.1 JPEG is already in Garage. v2 change: each
image carries its JPEG's ref, so v1's `jpeg_folder` copy step is gone.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.headtail import (
        HeadtailPredictionResult,
        PredictHeadtailImagesInput,
    )

__all__ = ["PredictHeadtailImagesWorkflow"]


@workflow.defn
class PredictHeadtailImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictHeadtailImagesInput
    ) -> List[HeadtailPredictionResult]:
        workflow.logger.info(
            "predicting headtail images dive_id=%s images=%d",
            payload.dive_id,
            len(payload.images),
        )
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_headtail_image",
                    image,
                    start_to_close_timeout=timedelta(minutes=15),
                    result_type=HeadtailPredictionResult,
                )
                for image in payload.images
            ]
        )
        return list(results)
