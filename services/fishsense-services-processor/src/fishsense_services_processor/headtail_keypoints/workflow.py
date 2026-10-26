"""The keypoint replay's GPU workflow: `PredictHeadtailKeypointsWorkflow`, one
`predict_headtail_keypoints` per frame, in parallel. A frame failing every
try is recorded as ``failed``; a worker that can't run the model -- no GPU,
or its weights cannot load (2026-10-24) -- fails the child (as the slate
pose's and the shadow calibration's do)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from fishsense_services_processor.worker_refusals import refusals
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.refusals import KEYPOINT_REFUSALS

    from fishsense_services_contracts.headtail_keypoints import (
        KeypointFramesInput,
        KeypointResult,
    )

__all__ = ["PredictHeadtailKeypointsWorkflow"]

FRAME_ATTEMPTS = 3
_WORKER_REFUSALS = KEYPOINT_REFUSALS
#: Before #364 (`worker_refusals`): what a run started then replays with.
_LEGACY_REFUSALS = ("NoGpuForKeypoints",)
_FRAME_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_attempts=FRAME_ATTEMPTS,
    non_retryable_error_types=list(_WORKER_REFUSALS),
)


@workflow.defn
class PredictHeadtailKeypointsWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: KeypointFramesInput) -> List[KeypointResult]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_headtail_keypoints",
                    frame,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_FRAME_RETRY,
                    result_type=KeypointResult,
                )
                for frame in request.frames
            ],
            return_exceptions=True,
        )
        results: List[KeypointResult] = []
        for frame, outcome in zip(request.frames, outcomes):
            if isinstance(outcome, ActivityError):
                cause = outcome.cause
                if isinstance(cause, ApplicationError) and cause.type in refusals(
                    _WORKER_REFUSALS, _LEGACY_REFUSALS
                ):
                    raise outcome
                workflow.logger.warning("keypoints capture=%s failed: %s",
                                        frame.capture_id, cause or outcome)  # fmt: skip
                results.append(KeypointResult(prediction_id=frame.prediction_id,
                                              capture_id=frame.capture_id, status="failed"))  # fmt: skip
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.append(outcome)
        return results
