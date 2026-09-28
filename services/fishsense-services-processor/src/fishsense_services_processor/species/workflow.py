"""Stage 2 workflow: draw each frame of each cluster, a cluster at a time.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
preprocess_species_images_workflow.py. Behaviour is v1's: for each cluster in
turn, every frame's `preprocess_species_image` runs together, each with a
5-minute start-to-close (not schedule-to-close: a fan-out queued behind the
per-image cap must not expire before it starts).

Inputs are resolved by the orchestrator's `PreprocessSpeciesImagesParentWorkflow`
(v1's api-worker parent); nothing here reads the database, the NAS or Label
Studio.

v2 changes: the payload is the processing contract (capture ids, and the
object refs the orchestrator issued), so the per-image input carries the
member itself rather than a checksum and v1's fixed output folder; and there is
no positional fallback for a payload without `cluster_members` (see
`fishsense_services_contracts.species`).
"""

import asyncio
from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.species import (
        PreprocessSpeciesImagesInput,
        SpeciesClusterMember,
    )

__all__ = ["PreprocessSpeciesImageInput", "PreprocessSpeciesImagesWorkflow"]


class PreprocessSpeciesImageInput(BaseModel):
    """One frame to draw: processor-internal (the workflow hands it to its own
    activity), so not part of the contract."""

    member: SpeciesClusterMember
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]


@workflow.defn
class PreprocessSpeciesImagesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessSpeciesImagesInput) -> None:
        workflow.logger.info(
            "preprocessing species dive_id=%s clusters=%d images=%d",
            payload.dive_id,
            len(payload.cluster_members),
            sum(len(group) for group in payload.cluster_members),
        )

        for group in payload.cluster_members:
            await asyncio.gather(
                *[
                    workflow.execute_activity(
                        "preprocess_species_image",
                        PreprocessSpeciesImageInput(
                            member=member,
                            camera_matrix=payload.camera_matrix,
                            distortion_coefficients=payload.distortion_coefficients,
                        ),
                        start_to_close_timeout=timedelta(minutes=5),
                    )
                    for member in group
                ]
            )
