"""The phone measurement's workflow: a chunk of phone captures, one activity
each, in parallel. A capture that fails every try is left out (it stays
unmeasured, and a later run asks again); the rest come back."""

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

    from fishsense_services_contracts.phone_measurement import (
        PhoneMeasured,
        PhoneMeasureInput,
    )

__all__ = ["CAPTURE_ATTEMPTS", "MeasurePhoneCapturesWorkflow"]

CAPTURE_ATTEMPTS = 3


@workflow.defn(name="MeasurePhoneCapturesWorkflow")
class MeasurePhoneCapturesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PhoneMeasureInput) -> List[PhoneMeasured]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "measure_phone_capture",
                    frame,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=10),
                        maximum_attempts=CAPTURE_ATTEMPTS,
                    ),
                    result_type=PhoneMeasured,
                )
                for frame in payload.frames
            ],
            return_exceptions=True,
        )
        results: List[PhoneMeasured] = []
        for frame, outcome in zip(payload.frames, outcomes):
            if isinstance(outcome, ActivityError):
                workflow.logger.warning("phone capture=%s left out: %s",
                                        frame.capture_id, outcome.cause or outcome)  # fmt: skip
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.append(outcome)
        return results
