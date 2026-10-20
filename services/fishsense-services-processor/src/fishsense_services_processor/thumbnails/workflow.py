"""The thumbnail workflow: one `make_thumbnail` per render.

Shaped as the render workflow (`renders.workflow`): every render at once,
`THUMBNAIL_ATTEMPTS` tries each; one that fails them all is left out of the
result -- still owed, so the next run makes it -- rather than failing the
others.
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

    from fishsense_services_contracts.thumbnails import (
        ThumbnailInput,
        ThumbnailResult,
        ThumbnailsInput,
    )

__all__ = ["THUMBNAIL_ATTEMPTS", "MakeThumbnailsWorkflow"]

THUMBNAIL_ATTEMPTS = 3
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10), maximum_attempts=THUMBNAIL_ATTEMPTS
)


@workflow.defn
class MakeThumbnailsWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: ThumbnailsInput) -> List[ThumbnailResult]:
        workflow.logger.info(
            "thumbnails tenant_id=%s renders=%d", payload.tenant_id, len(payload.frames)
        )
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "make_thumbnail",
                    ThumbnailInput(frame=frame, width=payload.width),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=_RETRY,
                    result_type=ThumbnailResult,
                )
                for frame in payload.frames
            ],
            return_exceptions=True,
        )
        results: List[ThumbnailResult] = []
        for frame, outcome in zip(payload.frames, outcomes):
            if isinstance(outcome, ActivityError):
                workflow.logger.warning(
                    "thumbnail render=%s left out after its tries: %s",
                    frame.render_id,
                    outcome.cause or outcome,
                )
            elif isinstance(outcome, BaseException):
                raise outcome  # cancellation, or a bug: not a render's failure
            else:
                results.append(outcome)
        return results
