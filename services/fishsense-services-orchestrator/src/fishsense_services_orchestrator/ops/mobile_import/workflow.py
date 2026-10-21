"""Import a FishSense Mobile export from the NAS into a tenant: on demand.

New in v2 (2026-10-21; the owner: CCFRP's summer 2025 data into ccfrp-ucsd).
A dry run unless told otherwise; an apply names the plan it reviewed:

    # the plan: how many captures, by day, phone and folder, what is left out
    temporal workflow start --task-queue fishsense_orchestrator \\
        --type ImportFsmExportWorkflow --workflow-id fsm-import-<name> \\
        --input '<FsmImportRequest JSON>'

    # import that plan
    ... --workflow-id fsm-import-<name>-apply \\
        --input '<the same JSON>' --input true --input '"<plan_sha256>"'

The request names the dataset, the tenant, the window by local date, the time
zone, and each phone's intrinsics with their source (the export has none).
The rules are in `ops.mobile_import.plan` and `.activities`. A refused import
(not a member, no bucket, no intrinsics, a changed plan) is not retried.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ops.mobile_import.contracts import (
        FsmImportReport,
        FsmImportRequest,
    )

__all__ = ["ImportFsmExportWorkflow"]

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=[
        "FsmImportRefused",
        "FsmImportPlanMismatch",
        "NotAMember",
        "NotAnImport",
    ],
)


@workflow.defn(name="ImportFsmExportWorkflow")
class ImportFsmExportWorkflow:
    # pylint: disable=too-few-public-methods
    """Plan (and, with ``apply`` and the reviewed digest, import) an export."""

    @workflow.run
    async def run(
        self,
        request: FsmImportRequest,
        apply: bool = False,
        expected_plan_sha256: str | None = None,
    ) -> FsmImportReport:
        if not apply:
            return await workflow.execute_activity(
                "plan_fsm_import",
                request,
                result_type=FsmImportReport,
                schedule_to_close_timeout=timedelta(hours=1),
                heartbeat_timeout=timedelta(minutes=20),
                retry_policy=_RETRY,
            )
        if not expected_plan_sha256:
            raise ApplicationError(
                "an apply names the plan it reviewed: pass its plan_sha256",
                type="FsmImportRefused",
                non_retryable=True,
            )
        return await workflow.execute_activity(
            "import_fsm_captures",
            args=(request, expected_plan_sha256),
            result_type=FsmImportReport,
            # ~550 captures, ~1 MB each, one at a time from the NAS.
            schedule_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(minutes=20),
            retry_policy=_RETRY,
        )
