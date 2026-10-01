"""The laser-depth workflow on the processor.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
compute_laser_depths_workflow.py: one activity, v1's timeouts (schedule to
close 1 h, heartbeat 2 min). v2 changes: the payload is the processing
contract's input, resolved by the orchestrator's
`ComputeLaserDepthsParentWorkflow`, which persists the result; no database
access here. A `ValueError` is not retried: the geometry is deterministic, so
a second attempt fails the same way.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.laser_depth import (
        ComputeLaserDepthsInput,
        ComputeLaserDepthsResult,
    )

__all__ = ["ComputeLaserDepthsWorkflow"]


@workflow.defn
class ComputeLaserDepthsWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: ComputeLaserDepthsInput) -> ComputeLaserDepthsResult:
        workflow.logger.info(
            "laser depths dive_id=%s captures=%d",
            payload.dive_id,
            len(payload.captures),
        )
        return await workflow.execute_activity(
            "compute_laser_depths",
            payload,
            schedule_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"]),
            result_type=ComputeLaserDepthsResult,
        )
