"""Unit tests for the shared NRP scaling helpers.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_k8s_scaling.py. They don't touch Kubernetes or Temporal -- they pin
the config resolution (disabled by default, required namespace, clamped
replica counts, defaults), the wedge check, and the replica-set call shape.

v2 changes, each pinned here:

* **stand up and tear down, never scale.** v1 patched the scale subresource of
  Deployments applied once by hand; NRP deleted them after two weeks (around
  2026-09-21) and five days of stages silently didn't run. v2 server-side
  applies the whole manifest with the target replica count, and deletes the
  Deployment for a target of zero;
* **the wedge check tolerates a missing Deployment** -- in v2 that is the
  ordinary idle state, and it is not a wedge;
* the settings are ``FISHSENSE_NRP_*`` (v1: Dynaconf ``[kubernetes]``), and a
  release image tag is required alongside the namespace;
* **the Deployment names come from the manifests** (deploy/nrp), not from
  settings. v1 derived the GPU and light names from a configurable base name so
  a renamed base carried the family (two v1 tests); with the manifests in the
  repo there is one place a name is written, so there is nothing to keep in
  step.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_orchestrator.nrp import scaling
from fishsense_services_orchestrator.nrp.scaling import NrpSettings

from ._nrp import (
    FALLBACK,
    GPU,
    LIGHT,
    MANIFEST_DIR,
    PER_IMAGE,
    FakeCluster,
    config,
    settings,
    without_wake,
)


def test_disabled_when_no_kubeconfig(monkeypatch):
    for name in ("KUBECONFIG_PATH", "NAMESPACE", "IMAGE_TAG"):
        monkeypatch.delenv(f"FISHSENSE_NRP_{name}", raising=False)
    assert scaling.resolve_scaling_config(NrpSettings()) is None
    assert scaling.resolve_scaling_config() is None

    # Namespace present but no kubeconfig_path → still disabled.
    assert scaling.resolve_scaling_config(NrpSettings(namespace="ns")) is None


def test_the_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("FISHSENSE_NRP_KUBECONFIG_PATH", "/run/secrets/nrp")
    monkeypatch.setenv("FISHSENSE_NRP_NAMESPACE", "e4e-fishsense")
    monkeypatch.setenv("FISHSENSE_NRP_IMAGE_TAG", "v2.0.0")
    monkeypatch.setenv("FISHSENSE_NRP_MANIFEST_DIR", str(MANIFEST_DIR))

    cfg = scaling.resolve_scaling_config()

    assert (cfg.kubeconfig_path, cfg.namespace, cfg.image_tag) == (
        "/run/secrets/nrp",
        "e4e-fishsense",
        "v2.0.0",
    )


def test_requires_namespace_when_kubeconfig_set():
    with pytest.raises(ValueError, match="NAMESPACE"):
        scaling.resolve_scaling_config(settings(namespace=None))


def test_requires_an_image_tag_when_kubeconfig_set():
    """v2: the orchestrator applies the manifests itself, with the release's
    image. Guessing (`latest`) would stand up whatever was pushed last."""
    with pytest.raises(ValueError, match="IMAGE_TAG"):
        scaling.resolve_scaling_config(settings(image_tag=None))


def test_a_missing_manifest_fails_at_startup(tmp_path):
    """The stage resolves its config when the worker starts, so a bad
    manifest directory stops the worker there, not at the first wake."""
    with pytest.raises(FileNotFoundError):
        scaling.resolve_scaling_config(settings(manifest_dir=tmp_path))


def test_defaults_when_only_required_keys_set():
    cfg = scaling.resolve_scaling_config(settings())
    assert cfg is not None
    assert cfg.kubeconfig_path == "/tmp/nrp.kubeconfig"
    assert cfg.namespace == "fishsense"
    assert cfg.per_image_deployment == PER_IMAGE
    assert cfg.active_replicas == 1
    assert cfg.idle_cooldown_minutes == 15


def test_the_deployment_names_come_from_the_manifests():
    cfg = scaling.resolve_scaling_config(settings())
    assert (
        cfg.per_image_deployment,
        cfg.light.deployment_name,
        cfg.gpu.deployment_name,
        cfg.gpu.fallback_deployment_name,
    ) == (PER_IMAGE, LIGHT, GPU, FALLBACK)


def test_active_replicas_clamped_to_ceiling():
    assert (
        scaling.resolve_scaling_config(settings(active_replicas=99)).active_replicas
        == scaling.MAX_ACTIVE_REPLICAS
    )


def test_active_replicas_clamped_to_floor():
    assert (
        scaling.resolve_scaling_config(settings(active_replicas=0)).active_replicas
        == scaling.MIN_ACTIVE_REPLICAS
    )


# -- the replica target: stand up or tear down (v1: the scale subresource) ----


def test_a_positive_target_applies_the_whole_manifest_server_side():
    """v2: create-or-update in one idempotent call, so a Deployment NRP has
    deleted comes back on the next wake instead of staying gone."""
    cluster = FakeCluster()
    cfg = config()

    scaling.set_deployment_replicas(cluster.kubernetes().apps, cfg, LIGHT, 2)

    ((name, body),) = cluster.applies
    assert name == LIGHT
    assert without_wake(body) == cfg.manifest(LIGHT).render(
        namespace="fishsense", image_tag="v1.2.3", replicas=2
    )
    field_manager, force, options = cluster.apply_options[0]
    assert field_manager == scaling.FIELD_MANAGER
    # The orchestrator owns these Deployments outright: an operator's edit is
    # overwritten on the next wake rather than failing it on a conflict.
    assert force is True
    assert options == {"_content_type": "application/apply-patch+yaml"}


def test_a_zero_target_deletes_the_deployment():
    """Never kept at zero replicas: an idle Deployment is exactly what NRP's
    two-week rule deletes out from under us."""
    cluster = FakeCluster()
    cluster.stand_up(LIGHT)

    scaling.set_deployment_replicas(cluster.kubernetes().apps, config(), LIGHT, 0)

    assert cluster.deletes == [LIGHT]
    assert not cluster.applies


def test_tearing_down_a_missing_deployment_is_not_an_error():
    """The ordinary idle state; the target is already met."""
    cluster = FakeCluster()
    assert not scaling.delete_deployment(cluster.kubernetes().apps, "fishsense", LIGHT)
    scaling.set_deployment_replicas(cluster.kubernetes().apps, config(), LIGHT, 0)
    assert not cluster.deletes


def test_other_api_errors_are_not_swallowed():
    apps = MagicMock()
    apps.delete_namespaced_deployment.side_effect = ApiException(status=403)
    apps.read_namespaced_deployment.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        scaling.delete_deployment(apps, "fishsense", LIGHT)
    with pytest.raises(ApiException):
        scaling.read_deployment(apps, "fishsense", LIGHT)


# -- the wedge check ------------------------------------------------------------


class _Deployment:
    """Minimal stand-in for V1Deployment's spec.replicas / status.ready_replicas."""

    def __init__(self, desired, ready):
        self.spec = SimpleNamespace(replicas=desired)
        self.status = SimpleNamespace(ready_replicas=ready)


def _api_returning(deployment):
    api = MagicMock()
    api.read_namespaced_deployment.return_value = deployment
    return api


@pytest.mark.parametrize(
    "desired,ready,expected",
    [
        # The prod wedge, 2026-08-14 onward: two pods asked for, none ever Ready
        # (CrashLoopBackOff on an expired Temporal cert). `ready_replicas` comes
        # back as None, not 0 — k8s omits the field rather than zeroing it, and
        # treating None as "unknown, assume healthy" is what would keep the
        # GPUs pinned.
        (2, None, True),
        (2, 0, True),
        (1, None, True),
        # A worker that has Ready pods is doing its job, however busy.
        (2, 1, False),
        (2, 2, False),
        (1, 1, False),
        # Already at zero: nothing is held, so there is nothing to reclaim and
        # this must not read as a wedge (it would make the sweeper claim a
        # tear-down it never performed).
        (0, None, False),
        (0, 0, False),
        (None, None, False),
    ],
)
def test_deployment_is_wedged(desired, ready, expected):
    api = _api_returning(_Deployment(desired, ready))
    assert scaling.deployment_is_wedged(api, "fishsense", "processor") is expected


def test_deployment_is_wedged_tolerates_a_status_less_deployment():
    """A freshly-created Deployment can come back with `status` unset."""
    deployment = SimpleNamespace(spec=SimpleNamespace(replicas=2), status=None)
    api = _api_returning(deployment)
    assert scaling.deployment_is_wedged(api, "fishsense", "processor") is True


def test_a_missing_deployment_is_not_wedged():
    """v2: torn down, or not stood up yet. Nothing is held, and nothing failed
    to start -- the GPU fallback must not count it."""
    cluster = FakeCluster()
    assert not scaling.deployment_is_wedged(cluster.kubernetes().apps, "fishsense", GPU)
    assert scaling.readiness(None) == scaling.Readiness(desired=0, ready_count=0)


# -- the GPU subsystem ----------------------------------------------------------


def test_gpu_replica_count_is_independent_of_the_cpu_one():
    """`active_replicas` sizes the per-image worker; each GPU pod holds a card
    on a contended cluster, so raising per-image throughput must not silently
    ask NRP for more GPUs."""
    cfg = scaling.resolve_scaling_config(settings(active_replicas=4))
    assert cfg.active_replicas == 4
    assert cfg.gpu.policy.active_replicas == 1

    cfg = scaling.resolve_scaling_config(settings(gpu_active_replicas=99))
    assert cfg.gpu.policy.active_replicas == scaling.MAX_ACTIVE_REPLICAS


def test_fallback_replicas_clamped():
    cfg = scaling.resolve_scaling_config(settings(gpu_fallback_replicas=99))
    assert cfg.gpu.policy.fallback_replicas == scaling.MAX_FALLBACK_REPLICAS
    cfg = scaling.resolve_scaling_config(settings(gpu_fallback_replicas=0))
    assert cfg.gpu.policy.fallback_replicas == 1


def test_max_start_failures_is_floored_at_one():
    """At 0 the pipeline would drop to CPU inference on the first observation
    and never actually try the GPU."""
    cfg = scaling.resolve_scaling_config(settings(gpu_max_start_failures=0))
    assert cfg.gpu.policy.max_start_failures == 1


def test_wedge_grace_cannot_outlast_the_start_timeout():
    """The activity waits out the start timeout and then observes. A longer
    grace would swallow every observation, no failure would ever be counted,
    and the CPU fallback could never trip."""
    cfg = scaling.resolve_scaling_config(
        settings(gpu_wedge_grace_minutes=60, gpu_start_timeout_seconds=120)
    )
    assert cfg.gpu.policy.wedge_grace.total_seconds() == 120


def test_sweep_targets_pair_each_deployment_with_the_queue_it_serves():
    cfg = scaling.resolve_scaling_config(settings())
    assert cfg.sweep_targets() == (
        (PER_IMAGE, PROCESSOR_TASK_QUEUE),
        (GPU, PROCESSOR_GPU_TASK_QUEUE),
        (FALLBACK, PROCESSOR_GPU_TASK_QUEUE),
        (LIGHT, PROCESSOR_LIGHT_TASK_QUEUE),
    )


def test_every_manifest_is_swept():
    """A Deployment missing from the sweep is never torn down -- on NRP, pods
    held around the clock (v1's rule, now checked against the manifests)."""
    cfg = scaling.resolve_scaling_config(settings())
    assert {name for name, _ in cfg.sweep_targets()} == set(cfg.manifests)


# -- the light subsystem --------------------------------------------------------


def test_the_light_worker_can_be_scaled_independently_of_the_cpu_one():
    """Separate replica knob because the two size against different things.
    `active_replicas` buys rawpy throughput on a memory-bound pod; the light
    worker is bound by neither and one replica serves every stage."""
    cfg = scaling.resolve_scaling_config(
        settings(active_replicas=3, light_active_replicas=1)
    )
    assert (cfg.active_replicas, cfg.light.active_replicas) == (3, 1)


def test_light_replicas_are_clamped_like_the_cpu_ones():
    cfg = scaling.resolve_scaling_config(settings(light_active_replicas=99))
    assert cfg.light.active_replicas == scaling.MAX_ACTIVE_REPLICAS


def test_light_scaling_defaults_to_one_replica():
    """The light stages are one dive per firing each; a second pod would add
    no throughput and NRP asks us to hold as little as possible."""
    cfg = scaling.resolve_scaling_config(settings())
    assert cfg.light.active_replicas == 1


def test_two_first_writes_of_the_gpu_state_both_land():
    """Two GPU wakes writing first-time state at once: both patches find no
    ConfigMap, one create wins, the other gets 409 AlreadyExists -- which now
    patches instead of failing the activity (review of
    foundation/nrp-processor-roles)."""
    from fishsense_services_orchestrator.nrp.gpu_fallback import GpuState

    calls = []

    class RacingCore:
        def patch_namespaced_config_map(self, name, namespace, body):
            calls.append("patch")
            if calls.count("patch") == 1:
                raise ApiException(status=404, reason="Not Found")

        def create_namespaced_config_map(self, namespace, body):
            calls.append("create")
            raise ApiException(status=409, reason="AlreadyExists")

    scaling.write_gpu_state(RacingCore(), "fishsense", "state", GpuState())

    assert calls == ["patch", "create", "patch"]
