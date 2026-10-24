"""The mask backfill's GPU workflow: a chunk of predictions, one activity
each, in parallel. A frame that fails every try is left out (its prediction
keeps no mask, and is asked again by a later run); a worker that cannot run
SAM -- no GPU, or its weights cannot load (2026-10-24) -- fails the chunk, as
the automatic frames stage's does."""

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

    from fishsense_services_contracts.refusals import SAM3_REFUSALS

    from fishsense_services_contracts.mask_backfill import (
        BackfilledMask,
        BackfillMasksInput,
    )

__all__ = ["BackfillMasksWorkflow", "FRAME_ATTEMPTS"]

FRAME_ATTEMPTS = 3
_WORKER_REFUSALS = SAM3_REFUSALS


@workflow.defn
class BackfillMasksWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: BackfillMasksInput) -> List[BackfilledMask]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "backfill_prediction_mask",
                    args=(payload.source, frame),
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=10),
                        maximum_attempts=FRAME_ATTEMPTS,
                        non_retryable_error_types=list(_WORKER_REFUSALS),
                    ),
                    result_type=BackfilledMask,
                )
                for frame in payload.frames
            ],
            return_exceptions=True,
        )
        results: List[BackfilledMask] = []
        for frame, outcome in zip(payload.frames, outcomes):
            if isinstance(outcome, ActivityError):
                cause = outcome.cause
                if (
                    isinstance(cause, ApplicationError)
                    and cause.type in _WORKER_REFUSALS
                ):
                    raise outcome
                workflow.logger.warning("mask backfill capture=%s left out: %s",
                                        frame.capture_id, cause or outcome)  # fmt: skip
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.append(outcome)
        return results
