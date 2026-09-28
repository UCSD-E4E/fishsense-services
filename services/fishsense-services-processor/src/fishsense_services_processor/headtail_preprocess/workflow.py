"""Stage 5.1 workflow: fan `preprocess_headtail_image` out over a dive's frames.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
preprocess_headtail_images_workflow.py. Behaviour is v1's: one activity per
frame, all at once, each with a 5-minute *start-to-close* budget (execution,
not queue wait: the dive-76 run expired on schedule-to-close while activities
waited for a slot).

Inputs are resolved by the orchestrator's `PreprocessHeadtailImagesParentWorkflow`;
this has no database or NAS access. v2 change: each frame carries its raw ref
and its JPEG ref, not a checksum and v1's hard-coded `output_folder`.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.headtail import (
        PreprocessHeadtailImageInput,
        PreprocessHeadtailImagesInput,
    )

__all__ = ["PreprocessHeadtailImagesWorkflow"]


@workflow.defn
class PreprocessHeadtailImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessHeadtailImagesInput) -> None:
        workflow.logger.info(
            "preprocessing headtail images dive_id=%s images=%d",
            payload.dive_id,
            len(payload.images),
        )
        await asyncio.gather(
            *[
                workflow.execute_activity(
                    "preprocess_headtail_image",
                    PreprocessHeadtailImageInput(
                        raw=image.raw,
                        jpeg=image.jpeg,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                )
                for image in payload.images
            ]
        )
