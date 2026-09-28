"""The GPU wake: stand up whatever can serve the GPU queue, and say which.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_ensure_gpu_worker_running_activity.py. Kubernetes is faked. What is
pinned here is the contract the predict parents depend on: which Deployment
ends up standing, what mode the parent is told to expect, and -- the point of
the whole feature -- that a GPU that will not start eventually hands its queue
to the CPU-only Deployment instead of stalling.

v2 changes, pinned below: the Deployment not in use is **deleted**, not held at
zero; a GPU Deployment that doesn't exist yet is a cold start, not a failure;
and the fallback's state lives in a ConfigMap, so it **survives the
Deployment's deletion** -- the case v1's annotations could not have survived.
v1's "never patches a pod template" tripwire becomes "never writes the state
into a Deployment": every apply is the rendered manifest, identical across
firings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.nrp.gpu_fallback import (
    FAILURES_KEY,
    FALLBACK_UNTIL_KEY,
    MODE_CPU_FALLBACK,
    MODE_GPU,
    MODE_UNAVAILABLE,
)

from ._nrp import FALLBACK, GPU, STATE, FakeCluster, config


def _activities(cluster: FakeCluster, **gpu) -> NrpActivities:
    return NrpActivities(
        config=config(gpu=gpu),
        kubernetes=cluster.kubernetes,
        poll_interval_seconds=0,
    )


async def _run(activities: NrpActivities) -> str:
    return await ActivityEnvironment().run(activities.ensure_gpu_processor_running)


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_state(cluster: FakeCluster, **data):
    cluster.config_maps[cluster.namespace, STATE] = dict(data)


async def test_noop_when_scaling_is_not_configured():
    """Locally and in e2e the processor is always up, so the parent should
    dispatch to the GPU queue exactly as before."""

    def _must_not(_path):
        pytest.fail("must not touch k8s")

    activities = NrpActivities(config=None, kubernetes=_must_not)
    assert await _run(activities) == MODE_GPU


async def test_cold_start_stands_the_gpu_deployment_up():
    """v2: the ordinary cold start is a Deployment that doesn't exist yet --
    neither ready nor wedged, so nothing is counted."""
    cluster = FakeCluster()
    assert await _run(_activities(cluster)) == MODE_GPU
    assert cluster.replicas(GPU) == 1
    assert not cluster.exists(FALLBACK)
    assert FAILURES_KEY not in cluster.state()


async def test_a_ready_gpu_keeps_the_fallback_down():
    cluster = FakeCluster()
    cluster.stand_up(GPU, replicas=1, ready=1)
    cluster.stand_up(FALLBACK, replicas=1, ready=1)
    assert await _run(_activities(cluster)) == MODE_GPU
    # v2: down means gone, not zero replicas.
    assert not cluster.exists(FALLBACK)


async def test_repeated_failed_starts_hand_the_queue_to_the_cpu_fallback():
    """The headline behavior: a GPU that never schedules must not stall the
    predict stage forever.

    Bounded so the test fails loudly rather than looping if the fallback ever
    stops tripping. The bound is generous on purpose — what matters is that it
    converges within a couple of hourly firings, not the exact count.
    """
    cluster = FakeCluster(broken={GPU})
    activities = _activities(cluster)

    firings = 0
    while True:
        firings += 1
        mode = await _run(activities)
        if mode == MODE_CPU_FALLBACK:
            break
        assert firings <= 4, "the GPU queue never fell back to CPU"

    assert not cluster.exists(GPU)
    assert cluster.replicas(FALLBACK) == 1
    assert FALLBACK_UNTIL_KEY in cluster.state()


async def test_the_count_survives_the_gpu_deployment_being_torn_down():
    """v2's reason for the ConfigMap. Between hourly firings the sweeper
    deletes the wedged GPU Deployment; with v1's annotations that would have
    erased the count on every sweep, and a GPU outage could never reach the
    fallback. The count must keep climbing across deletions."""
    cluster = FakeCluster(broken={GPU})
    activities = _activities(cluster)

    failures = []
    for _ in range(4):
        mode = await _run(activities)
        if mode == MODE_CPU_FALLBACK:
            break
        failures.append(int(cluster.state().get(FAILURES_KEY, 0)))
        # The sweeper, between firings.
        del cluster.deployments[GPU]
    else:
        pytest.fail("the GPU queue never fell back to CPU")

    assert failures == sorted(failures) and failures[-1] > 0, failures


async def test_the_fallback_window_is_held_then_released():
    cluster = FakeCluster()
    activities = _activities(cluster)

    # Already in fallback, window still open.
    _in_state(
        cluster,
        **{FALLBACK_UNTIL_KEY: _stamp(datetime.now(timezone.utc) + timedelta(hours=1))},
    )
    assert await _run(activities) == MODE_CPU_FALLBACK
    assert cluster.replicas(FALLBACK) == 1
    assert not cluster.exists(GPU)

    # Window expired → probe the GPU again, and drop the fallback.
    _in_state(
        cluster,
        **{
            FALLBACK_UNTIL_KEY: _stamp(
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ),
            FAILURES_KEY: "3",
        },
    )
    assert await _run(activities) == MODE_GPU
    assert cluster.replicas(GPU) == 1
    assert not cluster.exists(FALLBACK)
    assert FALLBACK_UNTIL_KEY not in cluster.state()
    assert FAILURES_KEY not in cluster.state()


async def test_reports_unavailable_when_the_fallback_cannot_start_either():
    """Nothing can serve the queue. The parent must be told, so it skips this
    firing instead of dispatching a child that would hang until its execution
    timeout hours later."""
    cluster = FakeCluster(broken={GPU, FALLBACK})
    _in_state(
        cluster,
        **{FALLBACK_UNTIL_KEY: _stamp(datetime.now(timezone.utc) + timedelta(hours=1))},
    )
    assert await _run(_activities(cluster)) == MODE_UNAVAILABLE


async def test_waits_for_a_pod_before_declaring_the_start_failed():
    """A cold pod that becomes Ready inside the start timeout is a success, not
    a failed attempt — otherwise every image pull would count toward the
    fallback threshold."""
    cluster = FakeCluster(broken={GPU})
    activities = _activities(cluster, start_timeout_seconds=60)
    read = cluster.read

    def _read(name, namespace):
        """The pod finishes pulling its image on the third poll."""
        if cluster.reads >= 3 and name == GPU:
            cluster.broken.discard(GPU)
            cluster.ready[GPU] = cluster.replicas(GPU)
        return read(name, namespace)

    cluster.read = _read

    assert await _run(activities) == MODE_GPU
    assert FAILURES_KEY not in cluster.state()


async def test_never_writes_the_state_into_a_deployment():
    """Tripwire (v1: never patch a pod template). The bookkeeping goes to the
    ConfigMap; every Deployment apply is the rendered manifest and nothing
    else, so re-applying across firings rolls no pods."""
    cluster = FakeCluster(broken={GPU})
    activities = _activities(cluster)
    await _run(activities)
    await _run(activities)

    assert cluster.config_map_writes, "expected the state to be written"
    assert all(name == STATE for name, _ in cluster.config_map_writes)
    gpu_bodies = [body for name, body in cluster.applies if name == GPU]
    assert len(gpu_bodies) >= 2
    assert all(body == gpu_bodies[0] for body in gpu_bodies)
    rendered = activities.config.manifest(GPU).render(
        namespace=cluster.namespace, image_tag="v1.2.3", replicas=1
    )
    assert gpu_bodies[0] == rendered
