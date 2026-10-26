"""The automatic-results GPU workflow: one `predict_automatic_frame` per frame.

Shaped as the head/tail predict workflow (`headtail_predict.workflow`):
every frame at once, 15 minutes start-to-close each (a cold pod's first
activity also pays two weight fetches and two model loads), one result per
frame, returned to the orchestrator's parent, which persists them.

**A frame that keeps failing is left out, not fatal** (2026-10-15 gap
audit). Under the default retry policy (no limit) one frame no worker could
finish -- a crash, an OOM -- retried until the parent's timeout killed this
workflow, and every other frame's work with it. Each frame now gets
`FRAME_ATTEMPTS` tries; one that fails them all is missing from the result,
which the orchestrator persists, recording each frame left out against the
frame (0044) -- not the dive. A worker's refusal (`NoGpuForSam3`) is not a
frame's: it fails this run with its cause (review of #85). Nor are weights
that cannot load, SAM 3.1's or the laser detector's (2026-10-24): left out,
every frame of the mission was recorded as a frame failure for a fault in
the pod, and given up as ``frame_failed`` at three runs. The refusals are
`fishsense_services_contracts.refusals.AUTOMATIC_FRAME_REFUSALS`, which the
orchestrator reads too.
"""

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

    from fishsense_services_contracts.refusals import AUTOMATIC_FRAME_REFUSALS

    from fishsense_services_contracts.automatic_results import (
        AutomaticFrameResult,
        PredictAutomaticFrameInput,
        PredictAutomaticFramesInput,
    )

__all__ = ["FRAME_ATTEMPTS", "PredictAutomaticFramesWorkflow"]

FRAME_ATTEMPTS = 3
#: The worker's refusals, not a frame's: every frame would refuse alike, so
#: the run fails with the cause rather than leaving each frame out.
_WORKER_REFUSALS = AUTOMATIC_FRAME_REFUSALS
#: Before #364 (`worker_refusals`): what a run started then replays with.
_LEGACY_REFUSALS = ("NoGpuForSam3",)
_FRAME_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_attempts=FRAME_ATTEMPTS,
    non_retryable_error_types=list(_WORKER_REFUSALS),
)


def _worker_refusal(error: ActivityError) -> bool:
    cause = error.cause
    return isinstance(cause, ApplicationError) and cause.type in refusals(
        _WORKER_REFUSALS, _LEGACY_REFUSALS
    )


@workflow.defn
class PredictAutomaticFramesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictAutomaticFramesInput
    ) -> List[AutomaticFrameResult]:
        workflow.logger.info(
            "automatic frames dive_id=%s frames=%d",
            payload.mission_id,
            len(payload.frames),
        )
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_automatic_frame",
                    PredictAutomaticFrameInput(
                        frame=frame,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                        laser_region=payload.laser_region,
                        camera_calibration_id=payload.camera_calibration_id,
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=_FRAME_RETRY,
                    result_type=AutomaticFrameResult,
                )
                for frame in payload.frames
            ],
            return_exceptions=True,
        )
        results: List[AutomaticFrameResult] = []
        for frame, outcome in zip(payload.frames, outcomes):
            if isinstance(outcome, ActivityError) and _worker_refusal(outcome):
                raise outcome
            if isinstance(outcome, ActivityError):
                workflow.logger.warning(
                    "automatic frame capture=%s left out after its tries: %s",
                    frame.capture_id,
                    outcome.cause or outcome,
                )
            elif isinstance(outcome, BaseException):
                raise outcome  # cancellation, or a bug: not a frame's failure
            else:
                results.append(outcome)
        return results
