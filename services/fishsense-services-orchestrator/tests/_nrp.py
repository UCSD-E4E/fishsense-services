"""Test helpers for the NRP stage: the repo's real manifests, and a fake cluster.

The fake models what the activities rely on and nothing more: a Deployment is
created or updated by a server-side apply and gone after a delete (reads of a
missing one raise 404, as the apiserver does); its pods go Ready as soon as
they are asked for, unless the Deployment is listed in ``broken`` -- which is
exactly the shape of an unschedulable GPU request, an exhausted quota, or a
CrashLoopBackOff. ConfigMaps take strategic-merge patches, where ``None``
removes a key. (Ported in spirit from fishsense-lite@77e8f8e5
tests/test_ensure_gpu_worker_running_activity.py `_Cluster`, which modelled
v1's scale subresource and annotations.)
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from fishsense_services_orchestrator.nrp.gpu_fallback import FallbackPolicy
from fishsense_services_orchestrator.nrp.scaling import (
    Kubernetes,
    NrpSettings,
    ScalingConfig,
    resolve_scaling_config,
)

#: The manifests the orchestrator deploys, as committed.
MANIFEST_DIR = Path(__file__).resolve().parents[3] / "deploy" / "nrp"

PER_IMAGE = "fishsense-processor"
LIGHT = "fishsense-processor-light"
GPU = "fishsense-processor-gpu"
FALLBACK = "fishsense-processor-gpu-cpu-fallback"
ALL_DEPLOYMENTS = [PER_IMAGE, GPU, FALLBACK, LIGHT]
STATE = "fishsense-processor-gpu-fallback"


def settings(**overrides) -> NrpSettings:
    values = {
        "kubeconfig_path": "/tmp/nrp.kubeconfig",
        "namespace": "fishsense",
        "image_tag": "v1.2.3",
        "manifest_dir": MANIFEST_DIR,
    }
    values.update(overrides)
    return NrpSettings(**values)


def config(*, gpu: dict | None = None, **overrides) -> ScalingConfig:
    """A resolved config over the real manifests. ``gpu`` overrides the GPU
    subsystem; by default it waits for no pod (a test says what is broken via
    `FakeCluster(broken=...)` rather than by timing)."""
    resolved = resolve_scaling_config(settings(**overrides))
    assert resolved is not None
    policy = FallbackPolicy(
        active_replicas=1,
        fallback_replicas=1,
        max_start_failures=3,
        wedge_grace=timedelta(0),
        fallback_window=timedelta(hours=3),
    )
    gpu_config = replace(resolved.gpu, start_timeout_seconds=0, policy=policy)
    if gpu:
        gpu_config = replace(gpu_config, **gpu)
    return replace(resolved, gpu=gpu_config)


def _not_found():
    return ApiException(status=404, reason="Not Found")


class _Apps:
    def __init__(self, cluster: FakeCluster):
        self._cluster = cluster

    def read_namespaced_deployment(self, name, namespace):
        return self._cluster.read(name, namespace)

    def patch_namespaced_deployment(
        self, name, namespace, body, *, field_manager=None, force=None, **kwargs
    ):
        self._cluster.apply(name, namespace, body, field_manager, force, kwargs)

    def delete_namespaced_deployment(self, name, namespace, **kwargs):
        self._cluster.delete(name, namespace)


class _Core:
    def __init__(self, cluster: FakeCluster):
        self._cluster = cluster

    def read_namespaced_config_map(self, name, namespace):
        if (namespace, name) not in self._cluster.config_maps:
            raise _not_found()
        return SimpleNamespace(data=dict(self._cluster.config_maps[namespace, name]))

    def patch_namespaced_config_map(self, name, namespace, body):
        if (namespace, name) not in self._cluster.config_maps:
            raise _not_found()
        self._cluster.config_map_writes.append((name, body))
        data = self._cluster.config_maps[namespace, name]
        for key, value in (body.get("data") or {}).items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value

    def create_namespaced_config_map(self, namespace, body):
        name = body["metadata"]["name"]
        if (namespace, name) in self._cluster.config_maps:
            raise ApiException(status=409, reason="AlreadyExists")
        self._cluster.config_map_writes.append((name, body))
        self._cluster.config_maps[namespace, name] = {
            k: v for k, v in (body.get("data") or {}).items() if v is not None
        }


class FakeCluster:
    def __init__(self, broken: set[str] | None = None, namespace: str = "fishsense"):
        self.namespace = namespace
        self.broken = set(broken or ())
        #: name -> the last body applied (what the apiserver would hold)
        self.deployments: dict[str, dict] = {}
        self.ready: dict[str, int] = {}
        self.config_maps: dict[tuple[str, str], dict[str, str]] = {}
        self.applies: list[tuple[str, dict]] = []
        self.apply_options: list[tuple[str | None, bool | None, dict]] = []
        self.deletes: list[str] = []
        self.config_map_writes: list[tuple[str, dict]] = []
        self.reads = 0

    # -- what a test sets up or inspects ------------------------------------

    def replicas(self, name: str) -> int:
        """0 when the Deployment doesn't exist -- v2's "scaled to zero"."""
        body = self.deployments.get(name)
        return 0 if body is None else body["spec"]["replicas"]

    def exists(self, name: str) -> bool:
        return name in self.deployments

    def stand_up(self, name: str, replicas: int = 1, ready: int | None = None):
        self.deployments[name] = {
            "metadata": {"name": name},
            "spec": {"replicas": replicas},
        }
        self.ready[name] = replicas if ready is None else ready

    def state(self) -> dict[str, str]:
        return self.config_maps.get((self.namespace, STATE), {})

    def kubernetes(self, _kubeconfig_path: str | None = None) -> Kubernetes:
        return Kubernetes(apps=_Apps(self), core=_Core(self))

    # -- the apiserver ---------------------------------------------------------

    def read(self, name, namespace):
        assert namespace == self.namespace
        self.reads += 1
        if name not in self.deployments:
            raise _not_found()
        ready = self.ready.get(name, 0)
        metadata = self.deployments[name].get("metadata", {})
        return SimpleNamespace(
            metadata=SimpleNamespace(annotations=metadata.get("annotations") or None),
            spec=SimpleNamespace(replicas=self.deployments[name]["spec"]["replicas"]),
            # k8s omits readyReplicas rather than sending 0.
            status=SimpleNamespace(ready_replicas=ready or None),
        )

    def apply(self, name, namespace, body, field_manager, force, kwargs):
        assert namespace == self.namespace
        assert body["metadata"]["name"] == name
        self.applies.append((name, copy.deepcopy(body)))
        self.apply_options.append((field_manager, force, kwargs))
        self.deployments[name] = copy.deepcopy(body)
        replicas = body["spec"]["replicas"]
        self.ready[name] = 0 if name in self.broken else replicas

    def delete(self, name, namespace):
        assert namespace == self.namespace
        if name not in self.deployments:
            raise _not_found()
        self.deletes.append(name)
        del self.deployments[name]
        self.ready.pop(name, None)


def without_wake(body: dict) -> dict:
    """A Deployment body less its `WOKEN_AT` stamp, which records *when* it
    was applied and so differs on every apply by design. What the stamp must
    and mustn't touch is pinned in test_nrp_manifests.py."""
    from fishsense_services_orchestrator.nrp.manifests import WOKEN_AT

    body = copy.deepcopy(body)
    annotations = body.get("metadata", {}).get("annotations")
    if annotations is not None:
        annotations.pop(WOKEN_AT, None)
        if not annotations:
            del body["metadata"]["annotations"]
    return body
