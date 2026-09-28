"""Stage 9 workflow: fan out `preprocess_slate_image` across a dive's slate
frames.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
preprocess_slate_images_workflow.py. Inputs are pre-resolved by the
orchestrator's `PreprocessSlateImagesParentWorkflow`, which selects the dive,
resolves its frames and stages their raw bytes and the slate PDF. Each frame
gets v1's 5-minute **start-to-close**: a schedule-to-close would count the
time a frame waits behind the per-image role's cap of 2.

v2 changes: each frame carries its raw ref and its composite's ref, and the
PDF is a ref (v1: checksums, a slate id and a fixed output folder).
"""

import asyncio
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.slate_calibration import (
        PreprocessSlateImagesInput,
    )
    from fishsense_services_processor.slate_preprocess.activities import (
        PreprocessSlateImageInput,
    )

__all__ = ["PreprocessSlateImagesWorkflow"]


@workflow.defn
class PreprocessSlateImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessSlateImagesInput) -> None:
        workflow.logger.info(
            "preprocessing slate images dive_id=%s images=%d slate=%s",
            payload.dive_id,
            len(payload.images),
            payload.slate_template_id,
        )

        await asyncio.gather(
            *[
                workflow.execute_activity(
                    "preprocess_slate_image",
                    PreprocessSlateImageInput(
                        capture_id=image.capture_id,
                        raw=image.raw,
                        jpeg=image.jpeg,
                        slate_pdf=payload.slate_pdf,
                        slate_dpi=payload.slate_dpi,
                        reference_points=payload.reference_points,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                )
                for image in payload.images
            ]
        )
