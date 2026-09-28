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

from fishsense_services_orchestrator.nrp.activities import NrpActivities

from ._nrp import LIGHT, PER_IMAGE, FakeCluster, config


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
    assert first == second
    assert cluster.replicas(LIGHT) == 2
