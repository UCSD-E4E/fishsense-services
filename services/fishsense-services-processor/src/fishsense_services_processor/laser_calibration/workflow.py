"""Stage 13's processor workflow: fit one dive's laser from its slate labels.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
perform_laser_calibration_workflow.py. An on-demand wrapper around
`perform_laser_calibration`, v1's 10-minute schedule-to-close and default
retry.

v2 changes: the input is the resolved `SlateCalibrationInput` (v1: a bare dive
id), and the output is a `LaserCalibrationResult` the orchestrator persists
(v1: the persisted row id, or None when the dive had no slate).
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.slate_calibration import (
        LaserCalibrationResult,
        SlateCalibrationInput,
    )

__all__ = ["PerformLaserCalibrationWorkflow"]


@workflow.defn
class PerformLaserCalibrationWorkflow:
    # pylint: disable=too-few-public-methods
    """Fit the laser for one dive and return the result, accepted or refused."""

    @workflow.run
    async def run(self, payload: SlateCalibrationInput) -> LaserCalibrationResult:
        return await workflow.execute_activity(
            "perform_laser_calibration",
            payload,
            schedule_to_close_timeout=timedelta(minutes=10),
            result_type=LaserCalibrationResult,
        )
