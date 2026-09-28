"""The processor's wake steps, for the parents, and the hourly sweeper.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/ (_dispatch.py's `wake_data_worker`,
`wake_light_worker`, `wake_gpu_worker`; _retry_policies.py's
`SCALING_RETRY_POLICY`; scale_down_idle_data_worker_workflow.py).

A parent that dispatches a child to a processor queue wakes that queue's role
first -- once it knows there is real work, so a quiet hour never wakes a pod.
The wakes are thin, individually callable steps (v1's reasoning: each parent
stays a readable top-to-bottom narrative, and the boilerplate lives here once).
They are no-ops when NRP scaling isn't configured.

v2 change: the processor is stood up and torn down (PLAN.md §3), so the
sweeper deletes Deployments; the steps' names say "processor", not v1's
"data worker".
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

__all__ = [
    "GPU_WAKE_TIMEOUT",
    "SCALING_RETRY_POLICY",
    "TearDownIdleProcessorsWorkflow",
    "WAKE_TIMEOUT",
    "wake_gpu_processor",
    "wake_light_processor",
    "wake_per_image_processor",
]

# Kubernetes control-plane calls. A transient NRP API blip should self-heal,
# but a real failure (bad kubeconfig, RBAC denied, expired token) should
# surface in seconds rather than burn the activity's whole schedule_to_close.
SCALING_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_attempts=3,
)

#: The per-image and light wakes' whole budget (schedule-to-close). A parent
#: whose schedule caps its run sums this in its worst case, so it is named
#: once, here, rather than repeated as a literal.
WAKE_TIMEOUT = timedelta(minutes=5)

#: The GPU wake's: longer because it waits for a pod (10 minutes by default)
#: and may then wait for a second one after flipping to the CPU fallback.
GPU_WAKE_TIMEOUT = timedelta(minutes=30)


async def wake_per_image_processor() -> None:
    """Stand the per-image processor up before its child lands on the queue.

    Idempotent -- converges on the configured replica count, never
    accumulates. Returns immediately, so the pod's cold start overlaps the
    parent's staging steps.
    """
    await workflow.execute_activity(
        "ensure_per_image_processor_running",
        schedule_to_close_timeout=WAKE_TIMEOUT,
        retry_policy=SCALING_RETRY_POLICY,
    )


async def wake_light_processor() -> None:
    """Stand the light processor up before its child lands on the light queue.

    A separate Deployment from the per-image one because that worker's cap of
    2 is a memory ceiling: one multi-image dispatch owns its queue, and in v1
    (2026-09-04) sub-second light work behind one expired on ScheduleToStart.
    Same shape as `wake_per_image_processor`.
    """
    await workflow.execute_activity(
        "ensure_light_processor_running",
        schedule_to_close_timeout=WAKE_TIMEOUT,
        retry_policy=SCALING_RETRY_POLICY,
    )


async def wake_gpu_processor() -> str:
    """Bring up a worker for the GPU queue, and report which one.

    Returns a value because the caller has a decision to make: the GPU queue
    is served by a GPU Deployment or, when that won't start, a CPU-only one
    running the same checkpoint -- and when neither can start, the honest
    answer is ``"unavailable"``. **A parent that gets that must not dispatch
    its child**: dispatching onto an unserved queue doesn't fail, it hangs
    until the child's execution timeout, hours later.

    Returns ``"gpu"``, ``"cpu_fallback"`` or ``"unavailable"`` (see
    `nrp.gpu_fallback`).
    """
    return await workflow.execute_activity(
        "ensure_gpu_processor_running",
        schedule_to_close_timeout=GPU_WAKE_TIMEOUT,
        heartbeat_timeout=timedelta(minutes=5),
        retry_policy=SCALING_RETRY_POLICY,
        result_type=str,
    )


@workflow.defn
class TearDownIdleProcessorsWorkflow:
    # pylint: disable=too-few-public-methods
    """Delete each processor Deployment whose queue is quiet.

    A thin wrapper around ``tear_down_idle_processors``. Scheduled near the
    end of the hour, after the parents' firings, so it doesn't race a parent
    that is still standing a processor up. Returns True if it tore anything
    down.
    """

    @workflow.run
    async def run(self) -> bool:
        return await workflow.execute_activity(
            "tear_down_idle_processors",
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=SCALING_RETRY_POLICY,
            result_type=bool,
        )
