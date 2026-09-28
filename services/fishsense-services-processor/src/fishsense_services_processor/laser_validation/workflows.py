"""The light role's laser workflows: thin wrappers the orchestrator dispatches.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
(evaluate_laser_auto_accept_workflow.py,
validate_laser_labels_for_dive_workflow.py, and the plan step of
remediate_laser_supersedes_workflow.py). The cross-worker split is v1's: the
orchestrator decides which dive and when; the processor does the maths.

v2 change: each is handed the dive's rows and returns what to write, because
the processor never reads or writes the database. Remediation plans ONE dive
here; the orchestrator's parent assembles the report and the digest.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.laser import (
        GATE_ACTIVITY_TIMEOUT,
        GATE_EXECUTION_TIMEOUT,
        GATE_QUEUE_WAIT_TIMEOUT,
        DivePlan,
        EvaluateLaserAutoAcceptInput,
        LaserAutoAcceptResult,
        LaserValidationResult,
        PlanLaserRemediationInput,
        ValidateLaserLabelsInput,
    )

__all__ = [
    "EvaluateLaserAutoAcceptWorkflow",
    "PlanLaserSupersedeRemediationWorkflow",
    "ValidateLaserLabelsForDiveWorkflow",
]

# v1's validator and plan budget: 10 minutes to run, 15 all told, a one-minute
# heartbeat to turn a silent hang into a diagnosable timeout.
_JUDGE_TIMEOUTS = {
    "schedule_to_close_timeout": timedelta(minutes=15),
    "start_to_close_timeout": timedelta(minutes=10),
    "heartbeat_timeout": timedelta(minutes=1),
}


@workflow.defn
class EvaluateLaserAutoAcceptWorkflow:
    # pylint: disable=too-few-public-methods
    """Judge a dive's predictions; return the verdicts and the summary."""

    @workflow.run
    async def run(self, payload: EvaluateLaserAutoAcceptInput) -> LaserAutoAcceptResult:
        # Queue wait and execution bounded SEPARATELY (v1's 2026-09-04 fix),
        # from the shared budget the orchestrator's parents size their child
        # timeout against -- never literals here.
        return await workflow.execute_activity(
            "evaluate_laser_auto_accept",
            payload,
            schedule_to_start_timeout=GATE_QUEUE_WAIT_TIMEOUT,
            start_to_close_timeout=GATE_EXECUTION_TIMEOUT,
            schedule_to_close_timeout=GATE_ACTIVITY_TIMEOUT,
            heartbeat_timeout=timedelta(minutes=1),
            result_type=LaserAutoAcceptResult,
        )


@workflow.defn
class ValidateLaserLabelsForDiveWorkflow:
    # pylint: disable=too-few-public-methods
    """One judgement of a dive's laser labels; returns what to write."""

    @workflow.run
    async def run(self, payload: ValidateLaserLabelsInput) -> LaserValidationResult:
        return await workflow.execute_activity(
            "validate_laser_labels_for_dive",
            payload,
            result_type=LaserValidationResult,
            **_JUDGE_TIMEOUTS,
        )


@workflow.defn
class PlanLaserSupersedeRemediationWorkflow:
    # pylint: disable=too-few-public-methods
    """One dive's remediation plan (its report row)."""

    @workflow.run
    async def run(self, payload: PlanLaserRemediationInput) -> DivePlan:
        return await workflow.execute_activity(
            "plan_laser_supersede_remediation",
            payload,
            result_type=DivePlan,
            **_JUDGE_TIMEOUTS,
        )
