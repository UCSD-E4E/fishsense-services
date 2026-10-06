"""The automatic-lengths workflow: one activity per dive (stage 14's shape)."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.automatic_results import (
        MeasureAutomaticInput,
        MeasureAutomaticResult,
    )

__all__ = ["MeasureAutomaticWorkflow"]


@workflow.defn
class MeasureAutomaticWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: MeasureAutomaticInput) -> MeasureAutomaticResult:
        return await workflow.execute_activity(
            "measure_automatic",
            payload,
            schedule_to_close_timeout=timedelta(hours=1),
            retry_policy=RetryPolicy(non_retryable_error_types=["ValueError"]),
            result_type=MeasureAutomaticResult,
        )
