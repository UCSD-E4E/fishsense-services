"""The label-free calibration workflow: one activity per dive."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.automatic_results import (
        AutomaticCalibrationResult,
        FitAutomaticCalibrationInput,
    )

__all__ = ["FitAutomaticCalibrationWorkflow"]


@workflow.defn
class FitAutomaticCalibrationWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: FitAutomaticCalibrationInput
    ) -> AutomaticCalibrationResult:
        workflow.logger.info(
            "label-free calibration dive_id=%s frames=%d",
            payload.dive_id,
            len(payload.frames),
        )
        return await workflow.execute_activity(
            "fit_automatic_calibration",
            payload,
            # Pairwise registration is quadratic in frames: ~30 slate frames
            # are ~450 pairs, seconds each at worst.
            schedule_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["ValueError"]
            ),
            result_type=AutomaticCalibrationResult,
        )
