"""Stage 14 (measure fish) parent workflow (orchestrator side).

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/measure_fish_parent_workflow.py.

Picks the oldest high-priority dive ready for fish measurement, wakes the
processor's light role on NRP, dispatches `MeasureFishWorkflow` to the light
queue, and persists what it returns -- binding each length to its fish. Each
run drains one dive; a backlog clears one dive per hour. No NAS or
object-store staging: measurement is maths on labels already stored.

v1's invariants, kept:

* `ScheduleOverlapPolicy.SKIP` on the schedule;
* the child's id is deterministic, with `ALLOW_DUPLICATE` -- never
  FAILED_ONLY, which swallowed every re-dispatch of a completed child, so a
  remediated dive could never be re-measured (dive 59, 2026-08-04). Safe
  because the persist is idempotent: a length is written only while it still
  answers open work;
* the wake runs only once the parent knows there is work.

v2 changes:

* the target is (tenant, dive), and the child's id is tenant-scoped
  (`measure-fish-{tenant}-{dive}`, PLAN.md §4.5/§9.4);
* v1's child did its own SDK reads and writes (species, fish, cluster
  bindings, measurements). Here the orchestrator resolves and persists, and
  the processor only computes;
* the run returns v1's counters, not only the dive.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
    from fishsense_services_contracts.measurement import MeasureFishResult
    from fishsense_services_orchestrator.laser_depth.activities import DiveTarget
    from fishsense_services_orchestrator.laser_depth.workflow import DB_FAIL_FAST
    from fishsense_services_orchestrator.measurement.activities import (
        MeasurementResolution,
        PersistedMeasurementRun,
    )
    from fishsense_services_orchestrator.nrp.workflow import wake_light_processor

__all__ = ["MeasureFishParentWorkflow", "MeasureFishRun"]


@dataclass(frozen=True)
class MeasureFishRun:
    """What one run did: v1's `MeasureFishResult` counters, for the dive it
    drained."""

    tenant_id: UUID
    dive_id: UUID
    measured: int = 0
    #: v1's `dropped_nan`, plus v2's zero lengths and unreadable species.
    refused: int = 0
    skipped_stale: int = 0
    fish_created: int = 0
    clusters_bound: int = 0
    skipped_already_measured: int = 0
    skipped_unmeasurable_species: int = 0
    missing_cluster: int = 0
    missing_laser_or_headtail: int = 0
    #: Refused on an earlier run, with nothing changed since.
    skipped_refused: int = 0
    #: Another firing's child was still running; it persists.
    dispatched_elsewhere: bool = False


@workflow.defn
class MeasureFishParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest high-priority dive ready for measurement and measure it
    on the processor. Returns what was done, or None when the cohort is
    empty."""

    @workflow.run
    async def run(self) -> Optional[MeasureFishRun]:
        target: Optional[DiveTarget] = await workflow.execute_activity(
            "select_next_dive_for_measurement",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=DB_FAIL_FAST,
            result_type=Optional[DiveTarget],
        )
        if target is None:
            return None

        resolution: MeasurementResolution = await workflow.execute_activity(
            "resolve_measurement_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=DB_FAIL_FAST,
            result_type=MeasurementResolution,
        )
        skipped = dict(
            skipped_already_measured=resolution.skipped_already_measured,
            skipped_unmeasurable_species=resolution.skipped_unmeasurable_species,
            missing_cluster=resolution.missing_cluster,
            missing_laser_or_headtail=resolution.missing_laser_or_headtail,
            skipped_refused=resolution.skipped_refused,
        )
        payload = resolution.payload
        if payload is None:
            return MeasureFishRun(target.tenant_id, target.dive_id, **skipped)
        workflow.logger.info(
            "dispatching fish measurement to the processor dive=%s captures=%d",
            target.dive_id,
            len(payload.captures),
        )

        await wake_light_processor()

        try:
            result: MeasureFishResult = await workflow.execute_child_workflow(
                "MeasureFishWorkflow",
                payload,
                id=f"measure-fish-{target.tenant_id}-{target.dive_id}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                execution_timeout=timedelta(hours=1),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=MeasureFishResult,
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "measure-fish for dive %s is still running under another firing; "
                "it persists",
                target.dive_id,
            )
            return MeasureFishRun(
                target.tenant_id, target.dive_id, dispatched_elsewhere=True, **skipped
            )

        persisted: PersistedMeasurementRun = await workflow.execute_activity(
            "persist_measurements",
            args=(target, payload.calibration.laser_calibration_id, result),
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["NotAMember"]
            ),
            result_type=PersistedMeasurementRun,
        )
        return MeasureFishRun(
            target.tenant_id,
            target.dive_id,
            measured=persisted.measured,
            refused=persisted.refused,
            skipped_stale=persisted.skipped_stale,
            fish_created=persisted.fish_created,
            clusters_bound=persisted.clusters_bound,
            **skipped,
        )
