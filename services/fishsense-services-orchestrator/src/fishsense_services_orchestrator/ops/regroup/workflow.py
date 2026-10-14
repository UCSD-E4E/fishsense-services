"""Regroup one dive's label-studio clusters in capture order: on demand.

New in v2 (2026-10-14). A dry run unless told otherwise; an apply names the
plan it reviewed:

    # the plan: every measured frame whose fish would change, and its digest
    temporal workflow start --task-queue fishsense_orchestrator \\
        --type RegroupDiveWorkflow \\
        --workflow-id regroup-<dive number> --input <dive number>

    # write that plan
    ... --workflow-id regroup-<dive number>-apply \\
        --input <dive number> --input true --input '"<plan_sha256>"'

The rules are in `ops.regroup.activities`. Here: one dive per run, and a
refused apply (unknown dive, plan or clusters changed) is not retried.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ops.regroup.contracts import RegroupReport

__all__ = ["RegroupDiveWorkflow"]

_REGROUP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
    non_retryable_error_types=[
        "DiveNotFound",
        "NotAMember",
        "RegroupPlanMismatch",
        "ClustersChanged",
        "RegroupRefused",
    ],
)


@workflow.defn
class RegroupDiveWorkflow:
    # pylint: disable=too-few-public-methods
    """Report (and, with ``apply`` and the reviewed digest, write) one dive's
    label-studio clusters, regrouped in capture order."""

    @workflow.run
    async def run(
        self,
        dive_number: int,
        apply: bool = False,
        expected_plan_sha256: str | None = None,
    ) -> RegroupReport:
        return await workflow.execute_activity(
            "regroup_dive",
            args=(dive_number, apply, expected_plan_sha256),
            result_type=RegroupReport,
            schedule_to_close_timeout=timedelta(minutes=10),
            retry_policy=_REGROUP_RETRY,
        )
