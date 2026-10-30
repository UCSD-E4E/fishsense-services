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
    from fishsense_services_contracts.calibration_borrow import (
        BorrowCheckInput,
        BorrowCheckReport,
    )
    from fishsense_services_contracts.calibration_v3 import (
        CalibrationV3Report,
        ReplayCalibrationInput,
    )

__all__ = [
    "CheckBorrowedBeamWorkflow",
    "FIT_TIMEOUT",
    "FitAutomaticCalibrationWorkflow",
    "ReplayAutomaticCalibrationV3Workflow",
]

#: The fit's budget, retries included. Pairwise registration is quadratic in
#: frames, capped at 40 (780 pairs, seconds each at worst); the activity
#: heartbeats throughout, so this bounds a fit that is slow, not silent.
FIT_TIMEOUT = timedelta(hours=2)


@workflow.defn
class FitAutomaticCalibrationWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: FitAutomaticCalibrationInput
    ) -> AutomaticCalibrationResult:
        workflow.logger.info(
            "label-free calibration dive_id=%s frames=%d",
            payload.mission_id,
            len(payload.frames),
        )
        return await workflow.execute_activity(
            "fit_automatic_calibration",
            payload,
            schedule_to_close_timeout=FIT_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["ValueError"]
            ),
            result_type=AutomaticCalibrationResult,
        )


@workflow.defn
class ReplayAutomaticCalibrationV3Workflow:
    """Calibration v3, read-only (2026-10-22): what it would make of the dive,
    and of draws of its slate frames. Nothing is stored."""

    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: ReplayCalibrationInput) -> CalibrationV3Report:
        return await workflow.execute_activity(
            "replay_automatic_calibration_v3",
            request,
            schedule_to_close_timeout=FIT_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
            result_type=CalibrationV3Report,
        )


@workflow.defn
class CheckBorrowedBeamWorkflow:
    """The checked borrow's check (v27, 2026-10-29): a neighbor's beam against
    the borrower's posed slate points. Nothing is stored; the orchestrator
    writes the borrow."""

    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: BorrowCheckInput) -> BorrowCheckReport:
        return await workflow.execute_activity(
            "check_borrowed_beam",
            request,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["ValueError"]
            ),
            result_type=BorrowCheckReport,
        )
