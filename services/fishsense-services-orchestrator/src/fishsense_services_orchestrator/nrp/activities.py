"""Stand the processor up for work, and tear it down when its queue is quiet.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/ (ensure_data_worker_running_activity,
ensure_light_worker_running_activity, ensure_gpu_worker_running_activity,
scale_down_data_worker_if_idle_activity). v1's were module functions reading
global settings; v2's are methods of a class given the resolved config, and
their Kubernetes clients and Temporal busy check are injected, so the tests
fake a cluster rather than patch modules.

**Wakes** (``ensure_*``) are called by a parent only once it knows there is real
work, so a quiet hour never wakes a pod. Each stands its Deployment up at an
absolute replica target -- a server-side apply of the manifest, so it creates
a Deployment that is gone (torn down, or deleted by NRP's two-week rule) as
readily as it updates one that is there. The per-image and light wakes return
at once, so the pod's cold start overlaps the parent's remaining steps; the
GPU wake waits, because it has a decision to make (see below).

**The sweeper** (``tear_down_idle_processors``) is the only thing that tears a
Deployment down -- parents only stand them up -- so overlapping parents can't
fight it. v2 **deletes** a quiet Deployment rather than scaling it to zero
(PLAN.md §3).

All four are no-ops when scaling isn't configured: locally and in e2e the
processor runs under compose, always on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.client import Client

from fishsense_services_orchestrator.nrp.gpu_fallback import (
    MODE_CPU_FALLBACK,
    MODE_GPU,
    MODE_UNAVAILABLE,
    GpuDecision,
    decide,
)
from fishsense_services_orchestrator.nrp.scaling import (
    Kubernetes,
    ScalingConfig,
    current_leaf,
    delete_deployment,
    deployment_is_wedged,
    kubernetes_apis,
    read_deployment,
    read_gpu_state,
    readiness,
    set_deployment_replicas,
    woken_at,
    write_gpu_state,
)

__all__ = ["NrpActivities", "build_busy_query", "task_queue_busy"]

#: (cooldown_minutes, task_queue) -> is it busy?
BusyCheck = Callable[[int, str], Awaitable[bool]]
#: (task queue, since) -> whether a workflow started on it after `since`.
UsedSinceCheck = Callable[[str, datetime], Awaitable[bool]]


def build_busy_query(cooldown_minutes: int, task_queue: str) -> str:
    """Temporal list-filter matching any workflow on ``task_queue`` that's
    Running or closed within the last ``cooldown_minutes``.

    A Running workflow has no ``CloseTime``, so the ``Running`` clause catches
    in-flight ones and the ``CloseTime >`` clause recently finished ones; an
    old, long-closed workflow matches neither. Querying by task queue means
    there is no workflow-type list to keep in sync.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'TaskQueue = "{task_queue}" '
        f'and (ExecutionStatus = "Running" or CloseTime > "{cutoff}")'
    )


async def task_queue_busy(
    client: Client, cooldown_minutes: int, task_queue: str
) -> bool:
    """True iff a workflow on ``task_queue`` is Running or closed within the
    last ``cooldown_minutes``."""
    async for _ in client.list_workflows(
        query=build_busy_query(cooldown_minutes, task_queue)
    ):
        return True
    return False


def build_used_since_query(task_queue: str, since: datetime) -> str:
    """Temporal list-filter matching any workflow started on ``task_queue``
    after ``since``: a wake's work has reached the queue."""
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'TaskQueue = "{task_queue}" and StartTime > "{stamp}"'


async def queue_used_since(client: Client, task_queue: str, since: datetime) -> bool:
    """True iff a workflow started on ``task_queue`` after ``since``."""
    async for _ in client.list_workflows(
        query=build_used_since_query(task_queue, since)
    ):
        return True
    return False


async def _used_through_the_worker_client(task_queue: str, since: datetime) -> bool:
    return await queue_used_since(activity.client(), task_queue, since)


async def _busy_through_the_worker_client(
    cooldown_minutes: int, task_queue: str
) -> bool:
    # The activity's own worker's client: v1 opened a fresh connection per
    # sweep; the worker's is already connected, to the same namespace.
    return await task_queue_busy(activity.client(), cooldown_minutes, task_queue)


class NrpActivities:
    def __init__(
        self,
        *,
        config: ScalingConfig | None,
        kubernetes: Callable[[str], Kubernetes] = kubernetes_apis,
        task_queue_busy: BusyCheck = _busy_through_the_worker_client,
        queue_used_since: UsedSinceCheck = _used_through_the_worker_client,
        poll_interval_seconds: float = 10,
    ) -> None:
        #: None when scaling is off: every activity no-ops.
        self.config = config
        self._kubernetes = kubernetes
        self._task_queue_busy = task_queue_busy
        self._queue_used_since = queue_used_since
        #: How often the GPU wake re-reads a Deployment while waiting for a
        #: pod to go Ready. Tests set it to 0.
        self._poll_interval_seconds = poll_interval_seconds

    def _apis(self) -> Kubernetes:
        return self._kubernetes(self.config.kubeconfig_path)

    async def _stand_up(self, name: str, replicas: int) -> None:
        def _apply() -> None:
            apis = self._apis()
            set_deployment_replicas(
                apis.apps,
                self.config,
                name,
                replicas,
                leaf_sha256=current_leaf(apis.core, self.config),
            )

        await asyncio.to_thread(_apply)
        activity.logger.info(
            "stood up %s/%s at %d replica(s)", self.config.namespace, name, replicas
        )

    # -- the per-image and light wakes -------------------------------------------

    @activity.defn(name="ensure_per_image_processor_running")
    async def ensure_per_image_processor_running(self) -> int:
        """Stand the per-image processor up at ``active_replicas``. Returns the
        target, or 0 when scaling is off."""
        if self.config is None:
            activity.logger.info(
                "NRP scaling not configured; assuming the per-image processor is up"
            )
            return 0
        await self._stand_up(
            self.config.per_image_deployment, self.config.active_replicas
        )
        return self.config.active_replicas

    @activity.defn(name="ensure_light_processor_running")
    async def ensure_light_processor_running(self) -> int:
        """Stand the light processor up at ``light_active_replicas``.

        The light queue holds the stages with no image bytes (clustering,
        calibration, measurement, ...). They are not simply on the per-image
        queue because that worker's cap of 2 is a memory ceiling: one
        multi-image preprocess owns both slots, and in v1 (2026-09-04)
        sub-second work behind it expired on ScheduleToStart. So this wakes a
        different Deployment, sized by a different knob.
        """
        if self.config is None:
            activity.logger.info(
                "NRP scaling not configured; assuming the light processor is up"
            )
            return 0
        light = self.config.light
        await self._stand_up(light.deployment_name, light.active_replicas)
        return light.active_replicas

    # -- the GPU wake ---------------------------------------------------------------

    def _decide_and_apply(self) -> GpuDecision:
        """One decision cycle, all of it on a worker thread.

        Reads the GPU Deployment once for readiness -- a missing one is a cold
        start, neither ready nor wedged -- and the fallback state from its
        ConfigMap; runs the pure policy; then stands up the chosen Deployment
        at an absolute count, tears the other down, and writes the bookkeeping
        back if it changed.
        """
        config = self.config
        apis = self._apis()
        status = readiness(
            read_deployment(apis.apps, config.namespace, config.gpu.deployment_name)
        )
        state = read_gpu_state(apis.core, config.namespace, config.state_config_map)

        decision = decide(
            state,
            now=datetime.now(timezone.utc),
            gpu_ready=status.ready,
            gpu_wedged=status.wedged,
            policy=config.gpu.policy,
        )

        leaf = current_leaf(apis.core, config)
        set_deployment_replicas(
            apis.apps,
            config,
            config.gpu.deployment_name,
            decision.gpu_replicas,
            leaf_sha256=leaf,
        )
        set_deployment_replicas(
            apis.apps,
            config,
            config.gpu.fallback_deployment_name,
            decision.fallback_replicas,
            leaf_sha256=leaf,
        )
        if decision.state != state:
            write_gpu_state(
                apis.core, config.namespace, config.state_config_map, decision.state
            )
        return decision

    def _is_ready(self, name: str) -> bool:
        return readiness(
            read_deployment(self._apis().apps, self.config.namespace, name)
        ).ready

    async def _wait_ready(self, name: str) -> bool:
        """Poll ``name`` until a pod is Ready or the start timeout elapses.

        Heartbeats so a long cold start doesn't look like a hung activity.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.gpu.start_timeout_seconds
        while True:
            if await asyncio.to_thread(self._is_ready, name):
                return True
            if loop.time() >= deadline:
                return False
            activity.heartbeat()
            await asyncio.sleep(self._poll_interval_seconds)

    @activity.defn(name="ensure_gpu_processor_running")
    async def ensure_gpu_processor_running(self) -> str:
        """Bring up a worker for the GPU queue; return ``gpu`` /
        ``cpu_fallback`` / ``unavailable``.

        * ``gpu`` -- the GPU Deployment is up (or scaling is off);
        * ``cpu_fallback`` -- the GPU Deployment has failed to start too many
          times; the CPU-only one is up and runs the same checkpoint slowly;
        * ``unavailable`` -- nothing came up inside the start timeout. **The
          caller must skip this firing**: a child dispatched onto a queue with
          no worker doesn't fail, it hangs until its execution timeout.

        **Why it waits.** The wait is what separates "the GPU is unavailable"
        from "the pod is still pulling its image". Without it every cold start
        would count toward the fallback threshold and a healthy cluster would
        drift onto CPU inference. The wedge grace is clamped to no more than
        the start timeout, so the observation after a timed-out wait counts.
        """
        config = self.config
        if config is None:
            activity.logger.info(
                "NRP scaling not configured; assuming the GPU processor is up"
            )
            return MODE_GPU

        decision = await asyncio.to_thread(self._decide_and_apply)
        activity.logger.info("GPU capacity: %s (%s)", decision.mode, decision.reason)

        chosen = (
            config.gpu.fallback_deployment_name
            if decision.mode == MODE_CPU_FALLBACK
            else config.gpu.deployment_name
        )
        if await self._wait_ready(chosen):
            return decision.mode

        if decision.mode == MODE_GPU:
            # The wait timed out, so this start failed. Observe it again — that
            # is what increments the counter, and it may flip us to the CPU
            # fallback.
            decision = await asyncio.to_thread(self._decide_and_apply)
            activity.logger.warning(
                "GPU processor did not become ready within %ds: %s",
                config.gpu.start_timeout_seconds,
                decision.reason,
            )
            if decision.mode == MODE_CPU_FALLBACK and await self._wait_ready(
                config.gpu.fallback_deployment_name
            ):
                return MODE_CPU_FALLBACK

        # Nothing is serving the queue. Say so rather than let the caller
        # dispatch a child that would hang until its execution timeout.
        activity.logger.warning(
            "no worker could be started for the GPU queue (%s / %s); skipping "
            "this firing - the cohort selector will pick the dive up again",
            config.gpu.deployment_name,
            config.gpu.fallback_deployment_name,
        )
        return MODE_UNAVAILABLE

    # -- the sweeper ------------------------------------------------------------------

    @activity.defn(name="tear_down_idle_processors")
    async def tear_down_idle_processors(self) -> bool:
        """Delete each processor Deployment whose queue is quiet.

        "Quiet" = no workflow on the queue *it* serves is Running, and none
        closed within ``idle_cooldown_minutes`` (so back-to-back dives don't
        thrash the pod). Each Deployment is swept against its own queue, so a
        busy per-image queue never keeps a GPU pod alive and vice versa. A busy
        queue keeps its Deployment only while it has a Ready pod, or is within
        the start timeout of a wake (still pulling its image): a wedged one is
        torn down anyway, since it can't drain what keeps it "busy".

        Writes **only** Deployments, never the GPU-fallback state. That
        separation is load-bearing: the fallback counts "pods wanted, none
        Ready" as a failed start, and if tearing down for ordinary idleness
        also cleared the count, a long GPU outage would reset its own counter
        every hour and never reach the fallback.

        Returns True if it deleted anything. A Deployment already gone is the
        ordinary idle state, and not counted.
        """
        config = self.config
        if config is None:
            activity.logger.info("NRP scaling not configured; nothing to tear down")
            return False

        targets = config.sweep_targets()

        # A recent wake is spared while its work hasn't reached the queue yet:
        # its parent may still be staging raws, and its pods may not be Ready
        # (`WOKEN_AT`). Only that long. Every wake re-stamps the Deployment and
        # stages wake hourly, so sparing any recent wake kept the GPU and light
        # processors up for a day and a half (2026-10-06/07); once the queue
        # has had work since the wake, idleness decides.
        def _stamps() -> dict[str, datetime | None]:
            apps = self._apis().apps
            return {name: woken_at(apps, config.namespace, name) for name, _ in targets}

        grace = timedelta(minutes=config.wake_grace_minutes)
        # The light and per-image wakes don't wait for a Ready pod, so their
        # child can reach the queue mid image pull: busy with no Ready pod is
        # a cold start for the start timeout after a wake, not a wedge. Only
        # that long -- stages wake hourly, so the wake grace would shield a
        # processor that never starts forever.
        cold_start = timedelta(seconds=config.gpu.start_timeout_seconds)
        now = datetime.now(timezone.utc)
        waiting, starting = set(), set()
        for (name, task_queue), woken in zip(
            targets, (await asyncio.to_thread(_stamps)).values()
        ):
            if woken is None:
                continue
            if now - woken < cold_start:
                starting.add(name)
            if now - woken < grace:
                if not await self._queue_used_since(task_queue, woken):
                    waiting.add(name)

        # Asked *after* "used since the wake?": a child that lands between the
        # two questions then reads as busy, never as used-but-not-busy.
        busy = set()
        # Each queue once, however many Deployments serve it.
        for task_queue in dict.fromkeys(q for _, q in targets):
            if await self._task_queue_busy(config.idle_cooldown_minutes, task_queue):
                busy.add(task_queue)

        def _sweep() -> list[tuple[str, str]]:
            """(deployment, outcome) pairs; one client for the whole pass."""
            apps = self._apis().apps
            outcomes = []
            for name, task_queue in targets:
                if name in waiting:
                    outcomes.append((name, "just-woken"))
                    continue
                if task_queue in busy:
                    if not deployment_is_wedged(apps, config.namespace, name):
                        outcomes.append((name, "busy"))
                        continue
                    if name in starting:
                        outcomes.append((name, "starting"))
                        continue
                deleted = delete_deployment(apps, config.namespace, name)
                if not deleted:
                    outcomes.append((name, "absent"))
                else:
                    outcomes.append((name, "wedged" if task_queue in busy else "idle"))
            return outcomes

        outcomes = await asyncio.to_thread(_sweep)

        for name, outcome in outcomes:
            if outcome == "just-woken":
                activity.logger.info(
                    "processor %s/%s was woken within %d minutes and no work has "
                    "reached its queue since; leaving it up for its parent",
                    config.namespace,
                    name,
                    config.wake_grace_minutes,
                )
            elif outcome == "starting":
                activity.logger.info(
                    "processor %s/%s has work but no Ready pod yet, within %ds of "
                    "its wake; leaving it to finish starting",
                    config.namespace,
                    name,
                    config.gpu.start_timeout_seconds,
                )
            elif outcome == "busy":
                activity.logger.info(
                    "task queue for %s/%s still busy or within %d-minute cooldown "
                    "and the worker is healthy; leaving it up",
                    config.namespace,
                    name,
                    config.idle_cooldown_minutes,
                )
            elif outcome == "wedged":
                # WARNING, not info: reclaiming the pods bounds the cost but
                # fixes nothing. Something is stopping this worker from
                # starting, and the backlog it leaves is real work not
                # happening. For the GPU Deployment this is also what the GPU
                # wake counts toward the CPU fallback, so a persistent wedge
                # there does eventually route around itself.
                activity.logger.warning(
                    "processor %s/%s has no Ready pod but its task queue is busy - "
                    "it cannot drain and was holding pods for nothing; deleted it. "
                    "Check pod status (CrashLoopBackOff? expired Temporal cert? "
                    "unschedulable?) - the next parent with work will stand it up.",
                    config.namespace,
                    name,
                )
            elif outcome == "idle":
                activity.logger.info(
                    "task queue idle; tore down %s/%s", config.namespace, name
                )

        return any(outcome in ("idle", "wedged") for _, outcome in outcomes)
