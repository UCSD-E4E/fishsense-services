"""Stage 14 (measure fish) workflow on the processor.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
measure_fish_workflow.py: one activity, v1's timeouts (schedule to close 1 h,
heartbeat 2 min). v2 changes: the payload is the processing contract's input,
resolved by the orchestrator's `MeasureFishParentWorkflow`, which binds fish
and persists the result; no database access here. A `ValueError` is not
retried: the geometry is deterministic.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.measurement import (
        MeasureFishInput,
        MeasureFishResult,
    )

__all__ = ["MeasureFishWorkflow"]


@workflow.defn
class MeasureFishWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: MeasureFishInput) -> MeasureFishResult:
        workflow.logger.info(
            "measure fish dive_id=%s captures=%d",
            payload.dive_id,
            len(payload.captures),
        )
        return await workflow.execute_activity(
            "measure_fish",
            payload,
            schedule_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"]),
            result_type=MeasureFishResult,
        )
