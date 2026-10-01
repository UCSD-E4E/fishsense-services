"""Stage 1 (dive-frame clustering) workflow on the processor.

Ported from fishsense-lite@a8b2c3bc services/fishsense-data-processing-workflow-
worker/src/fishsense_data_processing_workflow_worker/workflows/
dive_frame_clustering_workflow.py. Behaviour is v1's; v2 change: capture UUIDs,
from the processing contract.

Inputs are pre-resolved by the orchestrator's `ClusterDiveFramesParentWorkflow`,
which packs canonical captures' `(capture_id, taken_datetime)` pairs into a
`ClusterDiveFramesInput`. This workflow only delegates to the cluster activity
-- no database or NAS access on the processor side. Output is
`list[list[UUID]]` (capture ids per cluster); the parent persists them as
prediction clusters.
"""

from datetime import timedelta
from typing import List
from uuid import UUID

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import ClusterDiveFramesInput

__all__ = ["DiveFrameClusteringWorkflow"]


@workflow.defn
class DiveFrameClusteringWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: ClusterDiveFramesInput) -> List[List[UUID]]:
        workflow.logger.info(
            "clustering dive_id=%s images=%d",
            payload.dive_id,
            len(payload.images),
        )

        return await workflow.execute_activity(
            "cluster_dive_frames",
            payload.images,
            schedule_to_close_timeout=timedelta(minutes=10),
            result_type=List[List[UUID]],
        )
