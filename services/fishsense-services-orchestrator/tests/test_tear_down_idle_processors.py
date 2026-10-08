"""The hourly sweeper: tear each processor Deployment down when its queue is quiet.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_scale_down_data_worker_if_idle_activity.py and
test_scale_down_idle_data_worker_workflow.py. There are four Deployments over
three queues -- per-image, light, and the GPU worker with its CPU-only fallback
-- so the per-queue pairing is itself part of what is pinned here: a busy
per-image queue must not keep a GPU pod alive.

The Temporal-busy check and Kubernetes are faked; this pins: disabled → no-op;
busy → leave it; wedged-but-busy → tear down anyway; idle → tear down; that the
sweeper never writes the GPU-fallback state; and the Temporal list-filter.

v2 change: **tear down means delete**, never scale to zero (NRP deletes
Deployments older than two weeks). And a Deployment that is already gone is the
ordinary idle state: nothing to delete, and not reported as a tear-down.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_orchestrator.nrp import activities as sut
from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.nrp.gpu_fallback import FAILURES_KEY
from fishsense_services_orchestrator.nrp.manifests import WOKEN_AT
from fishsense_services_orchestrator.nrp.workflow import (
    TearDownIdleProcessorsWorkflow,
)

from ._nrp import (
    ALL_DEPLOYMENTS,
    FALLBACK,
    GPU,
    LIGHT,
    PER_IMAGE,
    STATE,
    FakeCluster,
    config,
)

ALL_QUEUES = {
    PROCESSOR_TASK_QUEUE,
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
}


def _busy(queues: set[str], asked: list | None = None):
    async def _check(_cooldown: int, task_queue: str) -> bool:
        if asked is not None:
            asked.append(task_queue)
        return task_queue in queues

    return _check


def _everything_up(cluster: FakeCluster, *, ready: bool = True) -> None:
    for name in ALL_DEPLOYMENTS:
        cluster.stand_up(name, replicas=1, ready=1 if ready else 0)


def _used(queues: set[str], asked: list | None = None):
    """`queue_used_since`: whether a workflow started on the queue after the
    wake; `asked` records (queue, since) pairs."""

    async def _check(task_queue: str, since: datetime) -> bool:
        if asked is not None:
            asked.append((task_queue, since))
        return task_queue in queues

    return _check


async def _sweep(
    cluster: FakeCluster, busy: set[str], asked=None, used: set[str] = frozenset()
) -> bool:
    activities = NrpActivities(
        config=config(),
        kubernetes=cluster.kubernetes,
        task_queue_busy=_busy(busy, asked),
        queue_used_since=_used(set(used)),
    )
    return await ActivityEnvironment().run(activities.tear_down_idle_processors)


async def test_noop_when_scaling_disabled():
    async def _must_not(*_a):
        pytest.fail("must not query Temporal when disabled")

    activities = NrpActivities(config=None, task_queue_busy=_must_not)
    assert (
        await ActivityEnvironment().run(activities.tear_down_idle_processors) is False
    )


async def test_does_not_tear_down_when_busy_and_the_worker_is_healthy():
    """Busy + a Ready pod = real work in flight. Leave it alone."""
    cluster = FakeCluster()
    _everything_up(cluster)

    assert await _sweep(cluster, ALL_QUEUES) is False
    assert not cluster.deletes


async def test_tears_down_a_wedged_worker_even_though_the_queue_looks_busy():
    """The feedback loop this exists to break.

    A processor that cannot produce a Ready pod (expired Temporal cert, bad
    image, unschedulable) never drains its queue — so every dispatched child
    sits Running, the queue never looks idle, and the Deployment holds GPUs
    around the clock. Exactly the state v1's prod was in from 2026-08-14.
    "Busy" is only a reason to keep the pods when the pods can make progress.
    """
    cluster = FakeCluster()
    _everything_up(cluster, ready=False)

    assert await _sweep(cluster, ALL_QUEUES) is True
    assert cluster.deletes == ALL_DEPLOYMENTS


async def test_tears_every_deployment_down_when_idle():
    cluster = FakeCluster()
    _everything_up(cluster)

    assert await _sweep(cluster, set()) is True
    assert cluster.deletes == ALL_DEPLOYMENTS
    assert not cluster.deployments


async def test_nothing_standing_is_nothing_to_tear_down():
    """v2: the ordinary idle hour. Every Deployment is already gone (torn
    down last hour, or deleted by NRP); none of that is an error, and the
    sweeper doesn't claim a tear-down it didn't do."""
    cluster = FakeCluster()

    assert await _sweep(cluster, set()) is False
    assert not cluster.deletes


async def test_a_busy_per_image_queue_does_not_keep_the_other_pods_alive():
    """The reason the split exists. Before it, one queue served everything, so
    hours of rectify work held a GPU the whole time — and would equally have
    held a light pod that had nothing to do."""
    cluster = FakeCluster()
    _everything_up(cluster)

    await _sweep(cluster, {PROCESSOR_TASK_QUEUE})

    assert cluster.deletes == [GPU, FALLBACK, LIGHT]


async def test_a_busy_gpu_queue_does_not_keep_the_per_image_worker_alive():
    cluster = FakeCluster()
    _everything_up(cluster)

    await _sweep(cluster, {PROCESSOR_GPU_TASK_QUEUE})

    assert cluster.deletes == [PER_IMAGE, LIGHT]


async def test_a_busy_light_queue_does_not_keep_the_per_image_worker_alive():
    """The converse of the split, and the one that costs real money: the light
    stages fire hourly and finish in seconds, so if their queue being busy held
    the per-image pod up, we would be paying for a 16 Gi rawpy worker to watch
    a line fit."""
    cluster = FakeCluster()
    _everything_up(cluster)

    await _sweep(cluster, {PROCESSOR_LIGHT_TASK_QUEUE})

    assert cluster.deletes == [PER_IMAGE, GPU, FALLBACK]


async def test_never_touches_the_gpu_fallback_state():
    """Tripwire. `gpu_fallback` counts "replicas wanted, no Ready pod" as a
    failed start. If routine idle tear-downs also rewrote (or deleted) that
    count, a multi-hour GPU outage would reset its own failure counter every
    hour and could never reach the CPU fallback — silently defeating the whole
    feature."""
    cluster = FakeCluster()
    _everything_up(cluster, ready=False)
    cluster.config_maps[cluster.namespace, STATE] = {FAILURES_KEY: "2"}

    await _sweep(cluster, set())

    assert not cluster.config_map_writes
    assert cluster.state() == {FAILURES_KEY: "2"}


async def test_queries_each_task_queue_once_not_once_per_deployment():
    """Two GPU Deployments share one queue; asking Temporal twice for the same
    answer is pure waste on an hourly job. Three queues, four Deployments."""
    cluster = FakeCluster()
    asked: list[str] = []

    await _sweep(cluster, set(), asked)

    assert sorted(asked) == sorted(ALL_QUEUES)


def test_busy_query_targets_the_task_queue_with_running_or_recent_close():
    query = sut.build_busy_query(15, PROCESSOR_TASK_QUEUE)
    assert f'TaskQueue = "{PROCESSOR_TASK_QUEUE}"' in query
    assert 'ExecutionStatus = "Running"' in query
    # A recent-close cutoff timestamp (RFC3339, Z-suffixed) is present.
    assert 'CloseTime > "20' in query and query.rstrip().endswith('Z")')


def test_busy_query_can_target_the_gpu_task_queue():
    query = sut.build_busy_query(15, PROCESSOR_GPU_TASK_QUEUE)
    assert f'TaskQueue = "{PROCESSOR_GPU_TASK_QUEUE}"' in query


# -- the workflow ---------------------------------------------------------------


@pytest.mark.parametrize("tore_down", [True, False])
async def test_workflow_returns_activity_result(tore_down: bool):
    """A thin wrapper: pin that it delegates to the activity and passes the
    result through."""
    calls: list = []

    @activity.defn(name="tear_down_idle_processors")
    async def stub_tear_down() -> bool:
        calls.append(True)
        return tore_down

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-processor-sweeper",
            workflows=[TearDownIdleProcessorsWorkflow],
            activities=[stub_tear_down],
        ):
            result = await env.client.execute_workflow(
                TearDownIdleProcessorsWorkflow.run,
                id=f"test-processor-sweeper-{uuid.uuid4()}",
                task_queue="test-processor-sweeper",
            )

    assert result is tore_down
    assert calls == [True]


# -- a wake is given time to reach its queue -----------------------------------------


async def test_a_processor_woken_moments_ago_is_left_while_its_parent_stages():
    """The race v1 had too: a parent wakes a processor, then stages raw frames
    for a while before its child is on the queue. A sweep in between saw a
    quiet queue and deleted the new Deployment, and the child then waited on
    an unserved queue. A wake stamps the Deployment, and the sweeper leaves a
    recent one alone."""
    cluster = FakeCluster()
    activities = NrpActivities(
        config=config(),
        kubernetes=cluster.kubernetes,
        task_queue_busy=_busy(set()),
        queue_used_since=_used(set()),
    )
    await ActivityEnvironment().run(activities.ensure_light_processor_running)

    await ActivityEnvironment().run(activities.tear_down_idle_processors)

    assert cluster.exists(LIGHT)


async def test_a_wake_older_than_the_grace_is_torn_down():
    cluster = FakeCluster()
    long_ago = datetime.now(timezone.utc) - timedelta(
        minutes=config().wake_grace_minutes + 1
    )
    cluster.deployments[LIGHT] = {
        "metadata": {"name": LIGHT, "annotations": {WOKEN_AT: long_ago.isoformat()}},
        "spec": {"replicas": 1},
    }
    cluster.ready[LIGHT] = 1

    await _sweep(cluster, busy=set())

    assert not cluster.exists(LIGHT)


def _woken(cluster: FakeCluster, name: str, minutes_ago: float) -> datetime:
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    cluster.deployments[name] = {
        "metadata": {"name": name, "annotations": {WOKEN_AT: at.isoformat()}},
        "spec": {"replicas": 1},
    }
    cluster.ready[name] = 1
    return at


async def test_a_recent_wake_whose_work_has_run_and_cooled_is_torn_down():
    """Every wake re-stamps the Deployment, and stages wake hourly, so a grace
    that protected any recent wake kept the GPU and light processors up for a
    day and a half (2026-10-06/07). The grace is for a parent still staging,
    before its child reaches the queue: once the queue has had work since the
    wake and has cooled, the wake was used, and the processor is idle."""
    cluster = FakeCluster()
    _woken(cluster, LIGHT, minutes_ago=30)

    await _sweep(cluster, busy=set(), used={PROCESSOR_LIGHT_TASK_QUEUE})

    assert not cluster.exists(LIGHT)


async def test_a_recent_wake_whose_child_has_not_arrived_is_left():
    cluster = FakeCluster()
    _woken(cluster, LIGHT, minutes_ago=30)

    await _sweep(cluster, busy=set(), used=set())

    assert cluster.exists(LIGHT)


async def test_the_grace_asks_about_work_since_the_wake_itself():
    cluster = FakeCluster()
    at = _woken(cluster, LIGHT, minutes_ago=30)
    asked: list = []
    activities = NrpActivities(
        config=config(),
        kubernetes=cluster.kubernetes,
        task_queue_busy=_busy(set()),
        queue_used_since=_used(set(), asked),
    )
    await ActivityEnvironment().run(activities.tear_down_idle_processors)

    assert (PROCESSOR_LIGHT_TASK_QUEUE, at) in asked


async def test_a_processor_left_for_its_wake_says_so(caplog):
    """It used to be the one outcome the sweeper didn't log, which is how the
    processors stayed up unnoticed."""
    cluster = FakeCluster()
    _woken(cluster, LIGHT, minutes_ago=5)

    with caplog.at_level("INFO"):
        await _sweep(cluster, busy=set(), used=set())

    assert any(
        "woken" in r.getMessage() and LIGHT in r.getMessage() for r in caplog.records
    )


def test_work_since_a_wake_is_any_workflow_started_on_the_queue_after_it():
    since = datetime(2026, 10, 7, 21, 42, 5, tzinfo=timezone.utc)
    assert sut.build_used_since_query("processor_light", since) == (
        'TaskQueue = "processor_light" and StartTime > "2026-10-07T21:42:05Z"'
    )


# -- a wake's cold start is not a wedge (code review of #52) -------------------------

START_TIMEOUT = {"start_timeout_seconds": 600}


def _cold(cluster: FakeCluster, name: str, minutes_ago: float) -> None:
    """Woken `minutes_ago`, its pod not Ready yet (pulling its image)."""
    _woken(cluster, name, minutes_ago)
    cluster.ready[name] = 0


async def _sweep_with(cluster: FakeCluster, busy, used, **config_overrides) -> None:
    activities = NrpActivities(
        config=config(**config_overrides),
        kubernetes=cluster.kubernetes,
        task_queue_busy=busy,
        queue_used_since=used,
    )
    await ActivityEnvironment().run(activities.tear_down_idle_processors)


async def test_a_fresh_wake_whose_child_arrived_during_its_cold_start_is_left():
    """The light and per-image wakes don't wait for a Ready pod, so the child
    can reach the queue while the pod still pulls its image. That ends the
    wake's grace -- but busy + no Ready pod is a cold start, not a wedge, and
    deleting it left the child waiting on an unserved queue."""
    cluster = FakeCluster()
    _cold(cluster, LIGHT, minutes_ago=2)

    await _sweep_with(
        cluster,
        _busy({PROCESSOR_LIGHT_TASK_QUEUE}),
        _used({PROCESSOR_LIGHT_TASK_QUEUE}),
        gpu=START_TIMEOUT,
    )

    assert cluster.exists(LIGHT)


async def test_a_wake_still_not_ready_after_the_start_timeout_is_wedged():
    """The cold-start allowance is the start timeout, not the wake grace:
    stages wake hourly, so a 90-minute allowance would keep a processor that
    never starts up forever -- the leak the wedge check exists to end."""
    cluster = FakeCluster()
    _cold(cluster, LIGHT, minutes_ago=11)

    await _sweep_with(
        cluster,
        _busy({PROCESSOR_LIGHT_TASK_QUEUE}),
        _used({PROCESSOR_LIGHT_TASK_QUEUE}),
        gpu=START_TIMEOUT,
    )

    assert not cluster.exists(LIGHT)


async def test_a_child_arriving_mid_sweep_does_not_get_its_processor_deleted():
    """A child that reaches the queue between the sweeper's two questions.
    Asked "busy?" first, then "used since the wake?", it reads not busy, yet
    used -- and the processor was deleted under the child. Asked the other
    way round, either answer keeps it: not used yet (the wake still waits) or
    used and so already busy."""
    cluster = FakeCluster()
    _woken(cluster, LIGHT, minutes_ago=5)
    arrived = {"child": False}

    async def _light_queue(*args) -> bool:
        """Either question about the light queue: the child lands right after
        the first one is answered."""
        if PROCESSOR_LIGHT_TASK_QUEUE not in args:
            return False
        seen = arrived["child"]
        arrived["child"] = True
        return seen

    await _sweep_with(cluster, _light_queue, _light_queue)

    assert cluster.exists(LIGHT)
