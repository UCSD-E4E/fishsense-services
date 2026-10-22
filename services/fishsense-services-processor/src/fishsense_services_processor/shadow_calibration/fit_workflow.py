"""The shadow calibration's fit workflow (light queue): one
`fit_shadow_calibration` per mission."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.shadow_calibration import (
        FitShadowCalibrationInput,
        ShadowFitResult,
    )

__all__ = ["FIT_TIMEOUT", "FitShadowCalibrationWorkflow"]

#: The fit: 2 sources x 2 variants, 500 bootstrap draws each, and the trim's
#: deleted residuals; heartbeating throughout.
FIT_TIMEOUT = timedelta(hours=1)


@workflow.defn
class FitShadowCalibrationWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: FitShadowCalibrationInput) -> ShadowFitResult:
        return await workflow.execute_activity(
            "fit_shadow_calibration",
            payload,
            schedule_to_close_timeout=FIT_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=2),
            result_type=ShadowFitResult,
        )
