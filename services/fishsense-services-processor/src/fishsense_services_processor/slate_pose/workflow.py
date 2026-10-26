"""Calibration v4's pose workflow (GPU queue): `SlatePoseFramesWorkflow`, one
`slate_pose_frame` per frame, in parallel. A frame failing every try is
recorded as ``failed``; a worker that can't run SAM -- no GPU, or its
weights cannot load (2026-10-24) -- fails the child, as the shadow
calibration's segmentation does."""

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

    from fishsense_services_contracts.refusals import SAM3_REFUSALS

    from fishsense_services_contracts.calibration_v4 import (
        SlatePose,
        SlatePoseFramesInput,
    )

__all__ = ["SlatePoseFramesWorkflow"]

FRAME_ATTEMPTS = 3
_WORKER_REFUSALS = SAM3_REFUSALS
#: Before #364 (`worker_refusals`): what a run started then replays with.
_LEGACY_REFUSALS = ("NoGpuForSam3",)
_FRAME_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_attempts=FRAME_ATTEMPTS,
    non_retryable_error_types=list(_WORKER_REFUSALS),
)


@workflow.defn
class SlatePoseFramesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: SlatePoseFramesInput) -> List[SlatePose]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "slate_pose_frame",
                    request.model_copy(update={"frames": [frame]}),
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=_FRAME_RETRY,
                    result_type=SlatePose,
                )
                for frame in request.frames
            ],
            return_exceptions=True,
        )
        results: List[SlatePose] = []
        for frame, outcome in zip(request.frames, outcomes):
            if isinstance(outcome, ActivityError):
                cause = outcome.cause
                if isinstance(cause, ApplicationError) and cause.type in refusals(
                    _WORKER_REFUSALS, _LEGACY_REFUSALS
                ):
                    raise outcome
                workflow.logger.warning("slate pose capture=%s failed: %s",
                                        frame.capture_id, cause or outcome)  # fmt: skip
                results.append(SlatePose(capture_id=frame.capture_id, status="failed"))
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.append(outcome)
        return results
