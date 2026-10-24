"""The model-class replay's GPU workflow: `ScoreSpeciesModelsWorkflow`, one
`score_species_models` per frame, in parallel; a frame failing every try is
recorded as ``failed``."""

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

    from fishsense_services_contracts.species_models import (
        SpeciesModelsInput,
        SpeciesModelsResult,
    )

__all__ = ["ScoreSpeciesModelsWorkflow"]

_FRAME_RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=3)


@workflow.defn
class ScoreSpeciesModelsWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: SpeciesModelsInput) -> List[SpeciesModelsResult]:
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "score_species_models",
                    request.model_copy(update={"frames": [frame]}),
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_FRAME_RETRY,
                    result_type=SpeciesModelsResult,
                )
                for frame in request.frames
            ],
            return_exceptions=True,
        )
        results: List[SpeciesModelsResult] = []
        for frame, outcome in zip(request.frames, outcomes):
            if isinstance(outcome, ActivityError):
                workflow.logger.warning("species models capture=%s failed: %s",
                                        frame.capture_id, outcome.cause or outcome)  # fmt: skip
                results.append(SpeciesModelsResult(prediction_id=frame.prediction_id,
                                                   capture_id=frame.capture_id,
                                                   status="failed"))  # fmt: skip
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results.append(outcome)
        return results
