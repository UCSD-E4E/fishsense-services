"""The species predict workflow: one `predict_species_image` per image.

New in v2 (no v1 counterpart), shaped as the head/tail predict workflow is
(`headtail_predict.workflow`): every image at once, each with 15 minutes
start-to-close (a cold pod's first activity also pays the weight fetch and the
BioCLIP load), and one result per image, returned to the orchestrator's
parent, which persists them. Nothing to stage: the head/tail JPEG is already
in Garage.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.species_prediction import (
        PredictSpeciesImageInput,
        PredictSpeciesImagesInput,
        SpeciesPredictionResult,
    )

__all__ = ["PredictSpeciesImagesWorkflow"]


@workflow.defn
class PredictSpeciesImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictSpeciesImagesInput
    ) -> List[SpeciesPredictionResult]:
        workflow.logger.info(
            "predicting species dive_id=%s images=%d candidates=%d",
            payload.dive_id,
            len(payload.images),
            len(payload.candidates),
        )
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "predict_species_image",
                    PredictSpeciesImageInput(
                        image=image, candidates=payload.candidates
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    result_type=SpeciesPredictionResult,
                )
                for image in payload.images
            ]
        )
        return list(results)
