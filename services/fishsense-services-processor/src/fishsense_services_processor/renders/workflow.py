"""The render workflow: one `render_capture` per capture of a mission.

Shaped as the automatic frames workflow (`automatic_frames.workflow`): every
capture at once, `RENDER_ATTEMPTS` tries each; a capture that fails them all
is left out of the result -- still owed, so the next run renders it -- rather
than failing the mission's other renders.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.renders import (
        RenderCaptureInput,
        RenderCapturesInput,
        RenderResult,
    )

__all__ = ["RENDER_ATTEMPTS", "RenderCapturesWorkflow"]

RENDER_ATTEMPTS = 3
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10), maximum_attempts=RENDER_ATTEMPTS
)


@workflow.defn
class RenderCapturesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: RenderCapturesInput) -> List[RenderResult]:
        workflow.logger.info(
            "renders mission_id=%s captures=%d", payload.mission_id, len(payload.frames)
        )
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "render_capture",
                    RenderCaptureInput(
                        frame=frame,
                        render_version=payload.render_version,
                        camera_calibration_id=payload.camera_calibration_id,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                    ),
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_RETRY,
                    result_type=RenderResult,
                )
                for frame in payload.frames
            ],
            return_exceptions=True,
        )
        results: List[RenderResult] = []
        for frame, outcome in zip(payload.frames, outcomes):
            if isinstance(outcome, ActivityError):
                workflow.logger.warning(
                    "render capture=%s left out after its tries: %s",
                    frame.capture_id,
                    outcome.cause or outcome,
                )
            elif isinstance(outcome, BaseException):
                raise outcome  # cancellation, or a bug: not a capture's failure
            else:
                results.append(outcome)
        return results
