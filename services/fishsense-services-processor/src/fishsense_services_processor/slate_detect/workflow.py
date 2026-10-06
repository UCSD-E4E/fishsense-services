"""The slate detect workflow: one `detect_slate_presence` per frame.

New in v2, shaped as laser prediction's is (`laser_predict.workflow`): every
frame at once with the dive's intrinsics, each with 15 minutes
start-to-close -- not schedule-to-close, since a dive's frames queue behind
the role's cap of 2 -- generous next to the ~20 s a frame takes (the raw
decode; the model is ~0.02 s on a GPU, ~0.3-0.7 s on a CPU), because a cold
pod's first activity also pays the weight fetch. One result per frame,
returned to the orchestrator's parent, which persists them.

The parent stages the raws first (`object_store.steps.stage_raw`) and names
this child through `raw_scratch_reader_id`, so cleanup waits for it.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.slate_presence import (
        DetectSlateImageInput,
        DetectSlateImagesInput,
        SlatePresenceResult,
    )

__all__ = ["DetectSlatePresenceWorkflow"]


@workflow.defn
class DetectSlatePresenceWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: DetectSlateImagesInput) -> List[SlatePresenceResult]:
        workflow.logger.info(
            "detecting slates dive_id=%s images=%d",
            payload.dive_id,
            len(payload.images),
        )
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "detect_slate_presence",
                    DetectSlateImageInput(
                        image=image,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    result_type=SlatePresenceResult,
                )
                for image in payload.images
            ]
        )
        return list(results)
