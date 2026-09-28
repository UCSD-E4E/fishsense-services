"""Hourly pass converging per-dive Label Studio projects onto the current configs.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/reconcile_labeling_configs_workflow.py.
A thin wrapper: the activity does the work (see
`labeling_configs.activities`). Idempotent -- a pass with no drift changes
nothing -- and scheduled at v1's :25, skipping on overlap (`ops.stage`).
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ops.labeling_configs.activities import (
        ReconcileLabelingConfigsResult,
    )

__all__ = ["ReconcileLabelingConfigsWorkflow"]


@workflow.defn
class ReconcileLabelingConfigsWorkflow:
    # pylint: disable=too-few-public-methods
    """Push each kind's current labeling config onto its per-dive projects."""

    @workflow.run
    async def run(self) -> ReconcileLabelingConfigsResult:
        return await workflow.execute_activity(
            "reconcile_labeling_configs",
            result_type=ReconcileLabelingConfigsResult,
            # Sized for a workspace-wide walk: one list call plus a detail
            # fetch per project whose listing omits `label_config`. The
            # activity heartbeats per project, so a slow Label Studio shows up
            # as a heartbeat timeout rather than silently eating the window.
            schedule_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
        )
