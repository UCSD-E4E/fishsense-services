"""The laser-colour backfill's processor workflow: `ReadLaserColoursWorkflow`,
one `read_laser_colour` per dot, in parallel. A dot failing every try is
recorded as ``failed`` (the keypoint replay's shape; no model, so no worker
refusal)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.laser_colour_backfill import (
        ColourFramesInput,
        ColourReading,
    )

__all__ = ["ReadLaserColoursWorkflow"]

FRAME_ATTEMPTS = 3
_FRAME_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10), maximum_attempts=FRAME_ATTEMPTS
)


@workflow.defn
class ReadLaserColoursWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: ColourFramesInput) -> List[ColourReading]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "read_laser_colour",
                    frame,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=_FRAME_RETRY,
                    result_type=ColourReading,
                )
                for frame in request.frames
            ],
            return_exceptions=True,
        )
        readings: List[ColourReading] = []
        for frame, outcome in zip(request.frames, outcomes):
            if isinstance(outcome, ActivityError):
                workflow.logger.warning("colour capture=%s failed: %s",
                                        frame.capture_id, outcome.cause or outcome)  # fmt: skip
                readings.append(ColourReading(prediction_id=frame.prediction_id,
                                              capture_id=frame.capture_id, status="failed"))  # fmt: skip
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                readings.append(outcome)
        return readings
