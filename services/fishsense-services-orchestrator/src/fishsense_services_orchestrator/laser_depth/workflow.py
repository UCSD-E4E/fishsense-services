"""The laser-depth parent workflow (orchestrator side).

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/compute_laser_depths_parent_workflow.py.

Picks the oldest high-priority dive with laser-depth work, wakes the
processor's light role on NRP, dispatches `ComputeLaserDepthsWorkflow` to the
light queue, and persists what it returns. Each run drains one dive; a
backlog clears one dive per hour. No NAS or object-store staging: the stage
reads labels, not bytes.

v1's invariants, kept:

* `ScheduleOverlapPolicy.SKIP` on the schedule stops two selectors picking the
  same dive concurrently;
* the child's id is deterministic, with `ALLOW_DUPLICATE` (never
  FAILED_ONLY: a completed child id must not block a re-run after a
  remediation -- dive 59); a parent that races past the schedule guard finds
  the other's child *running* and leaves the persist to it;
* the wake (v1's `ensure_light_worker_running_activity`) runs only once the
  parent knows there is work, so a quiet hour never wakes a pod.

v2 changes:

* the target is (tenant, dive), and the child's id is tenant-scoped
  (`compute-laser-depths-{tenant}-{dive}`, PLAN.md §4.5/§9.4);
* v1's child did its own reads and writes through the SDK. Here the
  orchestrator resolves the inputs and persists the result -- depths and
  refusals -- so the processor never touches the database;
* the run returns the counters, not only the dive: a green run is not proof
  the work was done (v1's "Draining a cohort by hand").
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
    from fishsense_services_contracts.laser_depth import ComputeLaserDepthsResult
    from fishsense_services_orchestrator.laser_depth.activities import (
        DiveTarget,
        LaserDepthResolution,
        PersistedLaserDepths,
    )
    from fishsense_services_orchestrator.nrp.workflow import wake_light_processor

__all__ = ["ComputeLaserDepthsParentWorkflow", "LaserDepthsRun", "DB_FAIL_FAST"]

# Selector and resolver are database round trips: one retry for a transient
# blip, then fail (v1's SDK_FAIL_FAST policy). A lost membership is final.
DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember"],
)


@dataclass(frozen=True)
class LaserDepthsRun:
    """What one run did: v1's `ComputeLaserDepthsResult` counters, for the
    dive it drained."""

    tenant_id: UUID
    dive_id: UUID
    computed: int = 0
    #: v1's `skipped_invalid_geometry`: images with no depth in front of the
    #: camera, now recorded as refusals.
    refused: int = 0
    skipped_current: int = 0
    #: Refused on an earlier run, with nothing changed since.
    skipped_refused: int = 0
    skipped_unusable_label: int = 0
    #: Results that no longer answered open work when persisted.
    skipped_stale: int = 0
    #: Another firing's child was still running; it persists.
    dispatched_elsewhere: bool = False


@workflow.defn
class ComputeLaserDepthsParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest high-priority dive needing laser depths and compute
    them on the processor. Returns what was done, or None when the cohort is
    empty."""

    @workflow.run
    async def run(self) -> Optional[LaserDepthsRun]:
        target: Optional[DiveTarget] = await workflow.execute_activity(
            "select_next_dive_for_laser_depth",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=DB_FAIL_FAST,
            result_type=Optional[DiveTarget],
        )
        if target is None:
            return None

        resolution: LaserDepthResolution = await workflow.execute_activity(
            "resolve_laser_depth_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=DB_FAIL_FAST,
            result_type=LaserDepthResolution,
        )
        skipped = dict(
            skipped_current=resolution.skipped_current,
            skipped_refused=resolution.skipped_refused,
            skipped_unusable_label=resolution.skipped_unusable_label,
        )
        payload = resolution.payload
        if payload is None:
            return LaserDepthsRun(target.tenant_id, target.dive_id, **skipped)
        workflow.logger.info(
            "dispatching laser depths to the processor dive=%s captures=%d",
            target.dive_id,
            len(payload.captures),
        )

        # On NRP nothing polls the light queue until the processor is stood
        # up (it is torn down when idle). Idempotent, and a no-op when NRP
        # scaling isn't configured; the child waits out the cold start.
        await wake_light_processor()

        try:
            result: ComputeLaserDepthsResult = await workflow.execute_child_workflow(
                "ComputeLaserDepthsWorkflow",
                payload,
                id=f"compute-laser-depths-{target.tenant_id}-{target.dive_id}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                execution_timeout=timedelta(hours=1),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=ComputeLaserDepthsResult,
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "compute-laser-depths for dive %s is still running under another "
                "firing; it persists",
                target.dive_id,
            )
            return LaserDepthsRun(
                target.tenant_id, target.dive_id, dispatched_elsewhere=True, **skipped
            )

        persisted: PersistedLaserDepths = await workflow.execute_activity(
            "persist_laser_depths",
            args=(target, payload.calibration.laser_calibration_id, result),
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["NotAMember"]
            ),
            result_type=PersistedLaserDepths,
        )
        return LaserDepthsRun(
            target.tenant_id,
            target.dive_id,
            computed=persisted.computed,
            refused=persisted.refused,
            skipped_stale=persisted.skipped_stale,
            **skipped,
        )
