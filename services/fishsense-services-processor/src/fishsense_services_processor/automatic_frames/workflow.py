"""The automatic-results GPU workflow: one `predict_automatic_frame` per frame.

Shaped as the head/tail predict workflow (`headtail_predict.workflow`):
every frame at once, 15 minutes start-to-close each (a cold pod's first
activity also pays two weight fetches and two model loads), one result per
frame, returned to the orchestrator's parent, which persists them.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.automatic_results import (
        AutomaticFrameResult,
        PredictAutomaticFrameInput,
        PredictAutomaticFramesInput,
    )

__all__ = ["PredictAutomaticFramesWorkflow"]


@workflow.defn
class PredictAutomaticFramesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictAutomaticFramesInput
    ) -> List[AutomaticFrameResult]:
        workflow.logger.info(
            "automatic frames dive_id=%s frames=%d",
            payload.dive_id,
            len(payload.frames),
        )
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_automatic_frame",
                    PredictAutomaticFrameInput(
                        frame=frame,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                        laser_region=payload.laser_region,
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    result_type=AutomaticFrameResult,
                )
                for frame in payload.frames
            ]
        )
        return list(results)
