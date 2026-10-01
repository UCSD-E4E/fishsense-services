"""Stage 1 parent workflow (orchestrator side).

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/cluster_dive_frames_parent_workflow.py.

Picks the oldest high-priority dive needing dive-frame clustering and
dispatches `DiveFrameClusteringWorkflow` to the processor's light queue. After
the child returns, persists its clusters as prediction clusters, so stage-2
species preprocessing has the cluster gate it depends on. Stage 1 has no NAS
or object-store staging -- clustering is pure maths on capture timestamps --
so the sequence is selector -> resolver -> child -> persist.

Cluster-correctness invariants (v1's):

* `ScheduleOverlapPolicy.SKIP` on the schedule stops two selectors picking the
  same dive concurrently;
* the child's id is deterministic (`cluster-{dive_id}`), so a parent that
  races past the schedule guard finds the other's child *running* and leaves
  the persist to it.

v2 changes:

* the target is (tenant, dive); ids are UUIDs; the queue is the processor's;
* **a completed child no longer blocks the persist.** v1 dispatched with
  ALLOW_DUPLICATE_FAILED_ONLY and returned on "already started", so a child
  that completed before its parent's persist failed wedged the dive: it was
  re-selected hourly, hit "already started", and never got clusters. v2 allows
  a duplicate of a *closed* child -- clustering is deterministic and cheap --
  and the persist is idempotent, so a re-run is harmless;
* no NRP scale-up step yet: the processor's scaling ports with the other
  worker duties (PLAN.md §6.2).
"""

from datetime import timedelta
from typing import List, Optional
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_LIGHT_TASK_QUEUE,
        ClusterDiveFramesInput,
    )
    from fishsense_services_orchestrator.clustering.activities import (
        ClusteringTarget,
    )

__all__ = ["ClusterDiveFramesParentWorkflow"]

# Selector and resolver are database round trips: one retry for a transient
# blip, then fail -- a consistent error is a bug, and should surface in seconds
# rather than after the activity's whole timeout (v1's SDK_FAIL_FAST policy). A
# lost membership is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember"],
)


@workflow.defn
class ClusterDiveFramesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest high-priority dive needing dive-frame clustering and
    dispatch its work to the processor. Returns the target processed, or None
    when the cohort is empty."""

    @workflow.run
    async def run(self) -> Optional[ClusteringTarget]:
        target: Optional[ClusteringTarget] = await workflow.execute_activity(
            "select_next_dive_for_clustering",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[ClusteringTarget],
        )
        if target is None:
            return None

        inputs: ClusterDiveFramesInput = await workflow.execute_activity(
            "resolve_clustering_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=ClusterDiveFramesInput,
        )
        workflow.logger.info(
            "dispatching dive-frame clustering to the processor dive=%s captures=%d",
            target.dive_id,
            len(inputs.images),
        )
        if not inputs.images:
            return target

        try:
            clusters: List[List[UUID]] = await workflow.execute_child_workflow(
                "DiveFrameClusteringWorkflow",
                inputs,
                id=f"cluster-{target.dive_id}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                execution_timeout=timedelta(minutes=15),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[List[UUID]],
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "cluster-%s is already running under another firing; it persists",
                target.dive_id,
            )
            return target

        await workflow.execute_activity(
            "persist_prediction_clusters",
            args=(target, clusters),
            schedule_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["InvalidClusters", "NotAMember"],
            ),
        )
        return target
