"""The NRP stage against a real Kubernetes apiserver.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_k8s_scaling_integration.py. Covers what the unit tests fake: the
activities creating, updating and deleting real Deployments from the repo's
manifests, the Kubernetes clients against a real kubeconfig (and its TLS), the
field semantics the wedge check depends on, and the GPU fallback's state
round-trip -- where a ``None`` value has to actually REMOVE the key, which only
a real apiserver can confirm. The Temporal query is exercised separately
(test_tear_down_query_integration.py).

Marked ``k8s`` and skipped unless ``FISHSENSE_K8S_ITEST_KUBECONFIG`` names a
kubeconfig. v2 change: not ``$KUBECONFIG``, which v1 read -- these tests create
and delete Deployments, and a developer's everyday kubeconfig may well point at
NRP. Point it at something disposable (kind, or k3s in Docker:
``docker run -d --privileged -p 127.0.0.1:6443:6443 rancher/k3s server``, then
copy ``/etc/rancher/k3s/k3s.yaml`` out).

v2 changes: the Deployments are not applied beforehand -- the activities stand
them up from deploy/nrp themselves, which is the point -- and "zero" means
"gone". Two tests are new, and only a real apiserver can answer them: a
re-apply of an unchanged manifest must not bump the Deployment's generation
(or every hourly wake would roll the pods), and a wake must bring back a
Deployment that has been deleted (NRP's two-week rule).

**Which Deployment a wedge test uses is load-bearing** (v1's note): a test
cluster has no GPU nodes, so the GPU Deployment -- which requests
``nvidia.com/gpu`` with a compute-capability affinity -- is *permanently*
unschedulable there, a deterministic wedge. The others are merely slow to fail
(an image pull), so asserting they are wedged would be a race.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.nrp import activities as activities_mod
from fishsense_services_orchestrator.nrp import scaling
from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.nrp.gpu_fallback import (
    FAILURES_KEY,
    FALLBACK_UNTIL_KEY,
    GpuState,
)
from fishsense_services_orchestrator.nrp.scaling import NrpSettings

from ._nrp import FALLBACK, GPU, MANIFEST_DIR, PER_IMAGE

pytestmark = pytest.mark.k8s


@pytest.fixture
def kubeconfig() -> str:
    path = os.environ.get("FISHSENSE_K8S_ITEST_KUBECONFIG")
    if not path or not Path(path).is_file():
        pytest.skip("FISHSENSE_K8S_ITEST_KUBECONFIG not set to a disposable cluster")
    return path


@pytest.fixture
def namespace() -> str:
    return os.environ.get("FISHSENSE_K8S_ITEST_NAMESPACE", "default")


@pytest.fixture
def config(kubeconfig, namespace):
    return scaling.resolve_scaling_config(
        NrpSettings(
            kubeconfig_path=kubeconfig,
            namespace=namespace,
            image_tag="itest",
            manifest_dir=MANIFEST_DIR,
            active_replicas=2,
            idle_cooldown_minutes=0,
            # No waiting for a pod: on a test cluster nothing ever becomes
            # Ready, so the default 600 s would just stall. With the grace at
            # 0 and a single permitted failure, the fallback flip is reached
            # in a bounded number of calls.
            gpu_start_timeout_seconds=0,
            gpu_wedge_grace_minutes=0,
            gpu_max_start_failures=1,
        )
    )


@pytest.fixture
def apis(config):
    apis = scaling.kubernetes_apis(config.kubeconfig_path)
    _clean(apis, config)
    yield apis
    _clean(apis, config)


def _clean(apis, config):
    for name in config.manifests:
        scaling.delete_deployment(apis.apps, config.namespace, name)
    try:
        apis.core.delete_namespaced_config_map(
            config.state_config_map, config.namespace
        )
    except Exception as exc:  # pylint: disable=broad-except
        if getattr(exc, "status", None) != 404:
            raise
    for name in config.manifests:
        _wait_gone(apis, config, name)


def _wait_gone(apis, config, name, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while scaling.read_deployment(apis.apps, config.namespace, name) is not None:
        assert time.monotonic() < deadline, f"{name} was not deleted"
        time.sleep(0.5)


def _replicas(apis, config, name) -> int | None:
    """``.spec.replicas``, or None when the Deployment is gone."""
    deployment = scaling.read_deployment(apis.apps, config.namespace, name)
    return None if deployment is None else deployment.spec.replicas


def _state(apis, config) -> dict:
    config_map = apis.core.read_namespaced_config_map(
        config.state_config_map, config.namespace
    )
    return config_map.data or {}


def _busy(value: bool):
    async def _check(_cooldown: int, _task_queue: str) -> bool:
        return value

    return _check


async def test_ensure_running_stands_the_real_deployment_up(config, apis):
    result = await ActivityEnvironment().run(
        NrpActivities(config=config).ensure_per_image_processor_running
    )
    assert result == 2
    assert _replicas(apis, config, PER_IMAGE) == 2


async def test_a_wake_brings_back_a_deleted_deployment(config, apis):
    """v2: NRP's two-week rule deletes the Deployment; the next wake's
    server-side apply creates it again, from the manifest."""
    nrp = NrpActivities(config=config)
    await ActivityEnvironment().run(nrp.ensure_per_image_processor_running)
    scaling.delete_deployment(apis.apps, config.namespace, PER_IMAGE)
    _wait_gone(apis, config, PER_IMAGE)

    await ActivityEnvironment().run(nrp.ensure_per_image_processor_running)

    assert _replicas(apis, config, PER_IMAGE) == 2


async def test_re_applying_an_unchanged_manifest_rolls_nothing(config, apis):
    """v2: every wake re-applies. An unchanged body must be a server-side
    no-op -- the same generation, so no new ReplicaSet and no pod restart."""
    nrp = NrpActivities(config=config)
    await ActivityEnvironment().run(nrp.ensure_per_image_processor_running)
    before = scaling.read_deployment(apis.apps, config.namespace, PER_IMAGE)

    await ActivityEnvironment().run(nrp.ensure_per_image_processor_running)
    after = scaling.read_deployment(apis.apps, config.namespace, PER_IMAGE)

    assert after.metadata.generation == before.metadata.generation
    assert (
        after.spec.template.spec.containers[0].image
        == "ghcr.io/ucsd-e4e/fishsense-services-processor:itest"
    )


async def test_tear_down_when_idle_deletes_the_real_deployment(config, apis):
    nrp = NrpActivities(config=config, task_queue_busy=_busy(False))
    await ActivityEnvironment().run(nrp.ensure_per_image_processor_running)

    result = await ActivityEnvironment().run(nrp.tear_down_idle_processors)

    assert result is True
    _wait_gone(apis, config, PER_IMAGE)


async def test_tear_down_leaves_a_busy_healthy_deployment_alone(
    config, apis, monkeypatch
):
    """Busy + able to make progress = leave it running.

    `deployment_is_wedged` is stubbed False because a test cluster cannot
    produce the honest version of "healthy": nothing there ever reaches Ready
    (an image that can't be pulled, a GPU that isn't there). The wedge test
    below needs no such stub, which is what makes it real.
    """
    monkeypatch.setattr(activities_mod, "deployment_is_wedged", lambda *_a: False)
    nrp = NrpActivities(config=config, task_queue_busy=_busy(True))
    await ActivityEnvironment().run(nrp.ensure_per_image_processor_running)

    result = await ActivityEnvironment().run(nrp.tear_down_idle_processors)

    assert result is False
    assert _replicas(apis, config, PER_IMAGE) == 2


async def test_deployment_is_wedged_against_a_real_apiserver(config, apis):
    """Pins the field semantics the wedge check depends on: with nothing
    Ready, `status.readyReplicas` is ABSENT (None), not 0 -- and reading None
    as "unknown, assume healthy" is what pinned v1's GPUs from 2026-08-14.
    v2: a Deployment that is gone is not wedged."""
    scaling.set_deployment_replicas(apis.apps, config, GPU, 1)
    assert scaling.deployment_is_wedged(apis.apps, config.namespace, GPU)

    scaling.set_deployment_replicas(apis.apps, config, GPU, 0)
    _wait_gone(apis, config, GPU)
    assert not scaling.deployment_is_wedged(apis.apps, config.namespace, GPU)


async def test_tear_down_reclaims_a_wedged_busy_deployment(config, apis):
    """The prod feedback loop, unmocked: the queue reports busy AND the
    Deployment cannot produce a Ready pod (here the GPU request is
    unschedulable; in v1's prod the Temporal cert had expired). The sweeper
    must reclaim it anyway."""
    scaling.set_deployment_replicas(apis.apps, config, GPU, 2)
    nrp = NrpActivities(config=config, task_queue_busy=_busy(True))

    result = await ActivityEnvironment().run(nrp.tear_down_idle_processors)

    assert result is True
    _wait_gone(apis, config, GPU)


async def test_the_state_patch_removes_a_key_on_a_real_apiserver(config, apis):
    """The fallback's "absent means healthy" contract rests on a None value
    REMOVING the key rather than storing an empty string -- strategic-merge
    semantics, which a fake can't confirm. v2: on the ConfigMap, which the
    first write creates."""
    scaling.write_gpu_state(
        apis.core, config.namespace, config.state_config_map, GpuState(failures=2)
    )
    assert _state(apis, config) == {FAILURES_KEY: "2"}

    scaling.write_gpu_state(
        apis.core, config.namespace, config.state_config_map, GpuState()
    )
    assert FAILURES_KEY not in _state(apis, config)
    assert (
        scaling.read_gpu_state(apis.core, config.namespace, config.state_config_map)
        == GpuState()
    )


async def test_gpu_worker_falls_back_to_cpu_against_a_real_cluster(config, apis):
    """The headline behaviour, end to end on a real apiserver.

    A test cluster can never schedule the GPU Deployment -- precisely the prod
    condition the fallback exists for. Repeated wakes must give up on it and
    bring the CPU-only Deployment up instead. Asserts the cluster rather than
    the returned mode: nothing reaches Ready, so the activity honestly reports
    `unavailable` throughout. What only a real apiserver proves is that the
    flip was persisted, from state read back from the cluster -- and in v2,
    that the GPU Deployment is gone rather than at zero.
    """
    nrp = NrpActivities(config=config, poll_interval_seconds=0)
    for _ in range(5):
        await ActivityEnvironment().run(nrp.ensure_gpu_processor_running)
        if _replicas(apis, config, FALLBACK) == 1:
            break
    else:
        pytest.fail("the GPU queue never fell back to the CPU Deployment")

    _wait_gone(apis, config, GPU)
    assert FALLBACK_UNTIL_KEY in _state(apis, config)
