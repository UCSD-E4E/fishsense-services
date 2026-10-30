"""The per-image and light wakes: stand the processor up before dispatching.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_ensure_data_worker_running_activity.py and
test_ensure_light_worker_running_activity.py. Kubernetes is faked; what is
pinned is the behavior -- which Deployment, how many replicas, and a no-op
(returning 0) when scaling isn't configured.

v2 change: a wake **applies the whole manifest** (server-side, idempotent)
rather than patching a replica count, so it brings back a Deployment that
isn't there -- torn down by the sweeper, or deleted by NRP's two-week rule,
which is what stranded v1 for five days.
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_orchestrator.nrp.activities import NrpActivities

from ._nrp import (
    LEAF,
    FakeCluster,
    LIGHT,
    PER_IMAGE,
    config,
    without_wake,
)


def _activities(cluster: FakeCluster, **overrides) -> NrpActivities:
    return NrpActivities(config=config(**overrides), kubernetes=cluster.kubernetes)


def _must_not_touch_kubernetes(_path):
    pytest.fail("must not build a k8s client")


async def test_noop_returns_zero_when_scaling_disabled():
    """Locally and in e2e the processor runs under compose and is always up,
    so waking it is meaningless rather than an error."""
    activities = NrpActivities(config=None, kubernetes=_must_not_touch_kubernetes)
    env = ActivityEnvironment()

    assert await env.run(activities.ensure_per_image_processor_running) == 0
    assert await env.run(activities.ensure_light_processor_running) == 0


async def test_scales_up_to_active_replicas():
    cluster = FakeCluster()
    activities = _activities(cluster, active_replicas=2)

    result = await ActivityEnvironment().run(
        activities.ensure_per_image_processor_running
    )

    assert result == 2
    assert [name for name, _ in cluster.applies] == [PER_IMAGE]
    assert cluster.replicas(PER_IMAGE) == 2


async def test_default_single_replica():
    cluster = FakeCluster()
    activities = _activities(cluster)

    result = await ActivityEnvironment().run(
        activities.ensure_per_image_processor_running
    )

    assert result == 1
    assert cluster.replicas(PER_IMAGE) == 1


async def test_it_scales_the_light_deployment_not_the_per_image_one():
    """The bug this guards: reading the per-image Deployment or
    `active_replicas` here would stand up the rawpy worker and leave the light
    queue unserved, so every light child would hang until its
    schedule-to-close."""
    cluster = FakeCluster()
    activities = _activities(cluster, active_replicas=3, light_active_replicas=1)

    result = await ActivityEnvironment().run(activities.ensure_light_processor_running)

    assert result == 1
    assert [name for name, _ in cluster.applies] == [LIGHT]
    assert cluster.replicas(LIGHT) == 1
    assert not cluster.exists(PER_IMAGE)


async def test_a_wake_brings_back_a_deployment_that_is_gone():
    """v2: nothing to scale is not a failure. The Deployment was torn down
    (or NRP deleted it); the wake creates it from the manifest, with the
    release's image."""
    cluster = FakeCluster()
    activities = _activities(cluster, image_tag="v9.9.9")
    assert not cluster.exists(LIGHT)

    await ActivityEnvironment().run(activities.ensure_light_processor_running)

    image = cluster.deployments[LIGHT]["spec"]["template"]["spec"]["containers"][0][
        "image"
    ]
    assert image.endswith(":v9.9.9")


async def test_overlapping_wakes_converge_rather_than_accumulate():
    """Two parents waking the same role apply the same body: an absolute
    target, never an increment, and no pod rolls for the second."""
    cluster = FakeCluster()
    activities = _activities(cluster, light_active_replicas=2)
    env = ActivityEnvironment()

    await env.run(activities.ensure_light_processor_running)
    await env.run(activities.ensure_light_processor_running)

    (_, first), (_, second) = cluster.applies
    assert without_wake(first) == without_wake(second)
    assert cluster.replicas(LIGHT) == 2


# -- the Temporal leaf the pods start on (ops.cert_sync) ---------------------------


@pytest.mark.parametrize(
    "wake, name",
    [
        ("ensure_per_image_processor_running", PER_IMAGE),
        ("ensure_light_processor_running", LIGHT),
    ],
)
async def test_a_wake_stamps_its_pods_with_the_leaf_they_mount(wake, name):
    """The pods mount the Temporal Secret as it is now, so the wake records
    which leaf that is on the pod template. The cert sync rolls a Deployment
    whose pods started on an older leaf -- and, by this stamp, leaves alone
    one stood up after the rotation rather than restarting it for nothing."""
    cluster = FakeCluster()
    cluster.push_leaf("sha-of-the-current-leaf")

    await ActivityEnvironment().run(getattr(_activities(cluster), wake))

    assert cluster.template_annotations(name)[LEAF] == "sha-of-the-current-leaf"


async def test_a_wake_before_the_first_cert_sync_stamps_nothing():
    """No Secret yet (a fresh namespace): nothing to record, and no reason to
    refuse the wake -- the first sync rolls the pods onto the leaf it pushes."""
    cluster = FakeCluster()

    await ActivityEnvironment().run(_activities(cluster).ensure_light_processor_running)

    assert LEAF not in cluster.template_annotations(LIGHT)


# -- workers already polling (#158) --------------------------------------------


async def test_a_per_image_worker_already_polling_leaves_nrp_alone():
    """#158: the lab render workers poll the per-image queue with up to 102
    slots, and a mission failed (2026-10-20, mission 422) because the NRP
    stand-up it didn't need timed out. With a per-image worker live, the queue
    is served: no stand-up."""
    asked = []

    async def pollers(task_queue):
        asked.append(task_queue)
        return 2

    activities = NrpActivities(
        config=config(), kubernetes=_must_not_touch_kubernetes,
        per_image_pollers=pollers,
    )  # fmt: skip

    assert (
        await ActivityEnvironment().run(activities.ensure_per_image_processor_running)
        == 0
    )
    assert asked == [PROCESSOR_TASK_QUEUE]


async def test_no_per_image_worker_polling_stands_nrp_up_as_before():
    cluster = FakeCluster()

    async def none_polling(task_queue):
        return 0

    activities = NrpActivities(config=config(active_replicas=2),
                               kubernetes=cluster.kubernetes,
                               per_image_pollers=none_polling)  # fmt: skip

    assert (
        await ActivityEnvironment().run(activities.ensure_per_image_processor_running)
        == 2
    )
    assert cluster.replicas(PER_IMAGE) == 2


async def test_a_poller_check_that_fails_stands_nrp_up_rather_than_fail_the_wake():
    """The check is an optimisation: when Temporal can't answer it, the wake
    does what it always did."""
    cluster = FakeCluster()

    async def broken(task_queue):
        raise RuntimeError("describe_task_queue failed")

    activities = NrpActivities(config=config(), kubernetes=cluster.kubernetes,
                               per_image_pollers=broken)  # fmt: skip

    assert (
        await ActivityEnvironment().run(activities.ensure_per_image_processor_running)
        == 1
    )
    assert cluster.replicas(PER_IMAGE) == 1


async def test_count_per_image_pollers_counts_the_processor_queue():
    """The wake step asks this after a failed wake: is anyone serving the
    queue anyway? It answers with scaling off too (the lab workers don't
    depend on NRP)."""
    asked = []

    async def pollers(task_queue):
        asked.append(task_queue)
        return 3

    activities = NrpActivities(
        config=None, kubernetes=_must_not_touch_kubernetes, per_image_pollers=pollers
    )

    assert await ActivityEnvironment().run(activities.count_per_image_pollers) == 3
    assert asked == [PROCESSOR_TASK_QUEUE]


@pytest.mark.integration
async def test_per_image_pollers_counts_a_live_per_image_worker_on_a_real_server():
    """Against a real Temporal (the dev server): a worker of this release
    polling as `.../per_image/cpu/<release>` counts; a GPU-role one on the
    same queue does not, nor a per-image one of another release (the lab
    workers on v0.72.0 under v0.74.0, 2026-10-30)."""
    import asyncio
    import uuid as _uuid

    from temporalio import activity as _activity
    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from importlib.metadata import version

    from fishsense_services_orchestrator.nrp.activities import per_image_pollers

    @_activity.defn(name="noop")
    async def noop() -> None:
        return None

    queue = f"per-image-{_uuid.uuid4()}"
    async with await WorkflowEnvironment.start_local() as env:
        release = version("fishsense-services-orchestrator")
        lab = Worker(env.client, task_queue=queue, activities=[noop],
                     identity=f"1@kastner-ml/per_image/cpu/{release}")  # fmt: skip
        other = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
        )  # one worker per queue per client
        gpu = Worker(other, task_queue=queue, activities=[noop],
                     identity=f"2@waiter/gpu/cuda/{release}")  # fmt: skip
        third = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
        )
        stale = Worker(third, task_queue=queue, activities=[noop],
                       identity="3@waiter/per_image/cpu/0.0.1")  # fmt: skip
        async with lab, gpu, stale:
            for _ in range(50):
                if await per_image_pollers(env.client, queue):
                    break
                await asyncio.sleep(0.2)
            assert await per_image_pollers(env.client, queue) == 1
