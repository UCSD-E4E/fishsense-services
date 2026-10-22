"""The shadow calibration's SAM workflow (GPU queue).

`SegmentSlateFramesWorkflow`: one `segment_slate_frame` per frame, in
parallel; a frame that fails every try is left out (its fit counts it as no
mask), but a worker that can't run SAM fails the child, as the automatic
frames stage does. The fit's workflow is in `fit_workflow` (its own module,
so each role's workflows name only that role's activities).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.shadow_calibration import (
        SegmentSlateFramesInput,
        SlateSegment,
    )

__all__ = ["SegmentSlateFramesWorkflow"]

FRAME_ATTEMPTS = 3
_WORKER_REFUSALS = ("NoGpuForSam3",)
_FRAME_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_attempts=FRAME_ATTEMPTS,
    non_retryable_error_types=list(_WORKER_REFUSALS),
)


@workflow.defn
class SegmentSlateFramesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: SegmentSlateFramesInput) -> List[SlateSegment]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "segment_slate_frame",
                    frame,
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=_FRAME_RETRY,
                    result_type=SlateSegment,
                )
                for frame in payload.frames
            ],
            return_exceptions=True,
        )
        results: List[SlateSegment] = []
        for frame, outcome in zip(payload.frames, outcomes):
            if isinstance(outcome, ActivityError):
                cause = outcome.cause
                if (
                    isinstance(cause, ApplicationError)
                    and cause.type in _WORKER_REFUSALS
                ):
                    raise outcome
                workflow.logger.warning("shadow segment capture=%s left out: %s",
                                        frame.capture_id, cause or outcome)  # fmt: skip
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.append(outcome)
        return results
