"""The head/tail predict workflow: one `predict_headtail_image` per image.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
predict_headtail_images_workflow.py. Behaviour is v1's: every image at once,
each with 15 minutes start-to-close (generous next to the ~0.6 s a prediction
takes, because a cold pod's first activity also pays the weight fetch and the
SAM 3.1 load), and one result per image, returned to the orchestrator's
parent, which persists them.

Nothing to stage: the stage-5.1 JPEG is already in Garage. v2 changes: each
image carries its JPEG's ref, so v1's `jpeg_folder` copy step is gone; and
**each image gets `IMAGE_ATTEMPTS` tries** (2026-10-24). v1's, and this
port's, set no retry policy, so an image no worker could finish (a crash, an
OOM) retried without limit, every 100 s at Temporal's default ceiling, until
the parent's 6 h child timeout -- holding the GPU queue the whole time. The
policy is the processor's other per-frame workflows' (`automatic_frames`,
`slate_pose`, ...): 10 s first backoff, three tries, and a worker's refusal
(`SAM3_REFUSALS`: no GPU, weights that cannot load) never retried. An image
that fails them all still fails the child, as before, only sooner: the
parent persists nothing of a failed child, so leaving it out instead would
need the parent to know it is owed.

**Replay.** The policy travels in the ScheduleActivityTask command, but
Temporal's determinism check matches a scheduled activity by its id and
type only, so a child started before this replays under it
(test_headtail_predict.py replays such a history, with a control); an
activity already scheduled keeps the policy it was scheduled with.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.refusals import SAM3_REFUSALS

    from fishsense_services_contracts.headtail import (
        HeadtailPredictionResult,
        PredictHeadtailImagesInput,
    )

__all__ = ["IMAGE_ATTEMPTS", "PredictHeadtailImagesWorkflow"]

IMAGE_ATTEMPTS = 3
_IMAGE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_attempts=IMAGE_ATTEMPTS,
    non_retryable_error_types=list(SAM3_REFUSALS),
)


@workflow.defn
class PredictHeadtailImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictHeadtailImagesInput
    ) -> List[HeadtailPredictionResult]:
        workflow.logger.info(
            "predicting headtail images dive_id=%s images=%d",
            payload.mission_id,
            len(payload.images),
        )
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_headtail_image",
                    image,
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=_IMAGE_RETRY,
                    result_type=HeadtailPredictionResult,
                )
                for image in payload.images
            ]
        )
        return list(results)
