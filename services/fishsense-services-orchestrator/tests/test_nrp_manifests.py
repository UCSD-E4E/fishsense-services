"""The processor's NRP manifests (deploy/nrp), and how the orchestrator renders them.

Ported from fishsense-lite@77e8f8e5 deploy/k8s/data-worker/deployment*.yaml
(four Deployments, one per role, the GPU queue served by a GPU one and a
CPU-only fallback). v1 applied them once with `kubectl apply -k` and only ever
scaled them; v2's orchestrator applies them itself, on every wake, with the
release's image tag and the replica target, and deletes them when idle. So the
files are now read by code, and what the code relies on is pinned here:

* each file is one Deployment, and the role its pod serves is the one its
  queue needs (the fallback serves the GPU role without a GPU);
* the files carry no replica count and no image tag -- the orchestrator owns
  both, and a count in the file would fight it;
* rendering is pure and deterministic, so re-applying on every wake rolls no
  pods unless the release changed.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from fishsense_services_orchestrator.nrp import manifests
from fishsense_services_orchestrator.nrp.manifests import Manifest, load_manifests
from fishsense_services_processor.worker import GRACEFUL_SHUTDOWN_TIMEOUT

from ._nrp import FALLBACK, GPU, LIGHT, MANIFEST_DIR, PER_IMAGE

LOADED = load_manifests(MANIFEST_DIR)
CPU_IMAGE = "ghcr.io/ucsd-e4e/fishsense-services-processor"
GPU_IMAGE = "ghcr.io/ucsd-e4e/fishsense-services-processor-gpu"


def _container(body: dict) -> dict:
    (container,) = body["spec"]["template"]["spec"]["containers"]
    return container


def _env(body: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in _container(body).get("env", [])}


def test_one_manifest_per_processor_deployment():
    assert set(LOADED) == {
        manifests.PER_IMAGE,
        manifests.LIGHT,
        manifests.GPU,
        manifests.GPU_CPU_FALLBACK,
    }
    assert {m.name for m in LOADED.values()} == {PER_IMAGE, LIGHT, GPU, FALLBACK}


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        (manifests.PER_IMAGE, "per_image"),
        (manifests.LIGHT, "light"),
        (manifests.GPU, "gpu"),
        # Same queue as the GPU one, and the same role: it is the GPU role on a
        # pod that has no GPU.
        (manifests.GPU_CPU_FALLBACK, "gpu"),
    ],
)
def test_each_pod_serves_its_role(kind, role):
    assert _env(LOADED[kind].body)["FISHSENSE_PROCESSOR_ROLE"] == role


def test_only_the_gpu_deployment_requests_a_gpu():
    """The fallback exists to be schedulable when no GPU is: a GPU request
    (or GPU affinity) there would make it fail for the same reasons."""
    for kind, manifest in LOADED.items():
        limits = _container(manifest.body)["resources"]["limits"]
        affinity = manifest.body["spec"]["template"]["spec"].get("affinity")
        if kind == manifests.GPU:
            assert limits["nvidia.com/gpu"] == "1"
            assert affinity
        else:
            assert "nvidia.com/gpu" not in limits, kind
            assert not affinity, kind


def test_the_fallback_runs_one_image_at_a_time():
    """CPU inference is slow and memory-hungry; concurrency buys little and
    risks the decode OOM (v1's cap of 1 on this Deployment)."""
    env = _env(LOADED[manifests.GPU_CPU_FALLBACK].body)
    assert env["FISHSENSE_PROCESSOR_MAX_CONCURRENT_ACTIVITIES"] == "1"


def test_the_torch_roles_run_the_gpu_image():
    """The fallback runs the same torch checkpoint on the CPU, so it needs the
    image built with the `torch` extra; the other two must not pull it."""
    images = {kind: _container(m.body)["image"] for kind, m in LOADED.items()}
    assert images == {
        manifests.PER_IMAGE: CPU_IMAGE,
        manifests.LIGHT: CPU_IMAGE,
        manifests.GPU: GPU_IMAGE,
        manifests.GPU_CPU_FALLBACK: GPU_IMAGE,
    }


def test_the_files_leave_replicas_and_the_tag_to_the_orchestrator():
    for manifest in LOADED.values():
        assert "replicas" not in manifest.body["spec"]
        assert ":" not in _container(manifest.body)["image"].rsplit("/", 1)[-1]


def test_a_pod_outlives_its_drain():
    """Tear-down deletes the Deployment: the pod is SIGTERMed and drains for
    the processor's graceful window, and must not be killed inside it."""
    for manifest in LOADED.values():
        spec = manifest.body["spec"]["template"]["spec"]
        assert (
            spec["terminationGracePeriodSeconds"]
            > GRACEFUL_SHUTDOWN_TIMEOUT.total_seconds()
        )


def test_the_processor_never_talks_to_kubernetes():
    """Only the orchestrator does, from outside the cluster: no token in the
    pod for a compromised dependency to use."""
    for manifest in LOADED.values():
        assert (
            manifest.body["spec"]["template"]["spec"]["automountServiceAccountToken"]
            is False
        )


def _gib(quantity: str) -> float:
    units = {"Gi": 1, "Mi": 1 / 1024}
    return float(quantity[:-2]) * units[quantity[-2:]]


def test_the_ephemeral_limit_covers_every_volume():
    """A volume filling to its own cap must not trip the container's overall
    limit and get the pod evicted first (v1's rule; v1's GPU pods broke it,
    with a 12Gi weights cache under a 7Gi limit)."""
    for kind, manifest in LOADED.items():
        spec = manifest.body["spec"]["template"]["spec"]
        volumes = sum(
            _gib(v["emptyDir"]["sizeLimit"]) for v in spec["volumes"] if "emptyDir" in v
        )
        limit = _gib(
            _container(manifest.body)["resources"]["limits"]["ephemeral-storage"]
        )
        assert limit >= volumes, kind


def test_the_pods_run_as_the_images_numeric_user():
    """The image's USER is a name (`app`, uid 10001 -- see the Dockerfile).
    With `runAsNonRoot` and no numeric `runAsUser`, kubelet cannot verify a
    name is non-root and refuses to start the container
    (CreateContainerConfigError): a pod that never goes Ready."""
    for kind, manifest in LOADED.items():
        security = manifest.body["spec"]["template"]["spec"]["securityContext"]
        assert security["runAsNonRoot"] is True, kind
        assert security["runAsUser"] == 10001, kind


def test_the_selector_matches_the_pods():
    for manifest in LOADED.values():
        selector = manifest.body["spec"]["selector"]["matchLabels"]
        labels = manifest.body["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items()


# -- rendering --------------------------------------------------------------------


def test_render_sets_the_namespace_the_replicas_and_the_release_tag():
    body = LOADED[manifests.LIGHT].render(
        namespace="e4e-fishsense", image_tag="v2.3.4", replicas=2
    )
    assert body["metadata"]["namespace"] == "e4e-fishsense"
    assert body["spec"]["replicas"] == 2
    assert _container(body)["image"] == f"{CPU_IMAGE}:v2.3.4"


def test_render_is_deterministic_and_leaves_the_manifest_alone():
    """Applied on every wake: an identical body is a server-side no-op, and
    anything that varied per call (a timestamp in the pod template) would roll
    the pods every hour."""
    manifest = LOADED[manifests.GPU]
    before = copy.deepcopy(manifest.body)
    first = manifest.render(namespace="ns", image_tag="v1", replicas=1)
    second = manifest.render(namespace="ns", image_tag="v1", replicas=1)
    assert first == second
    assert manifest.body == before


def _manifest_file(tmp_path, body: dict):
    path = tmp_path / "x.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


def _minimal(**spec) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "x"},
        "spec": {
            "template": {"spec": {"containers": [{"name": "w", "image": "img"}]}},
            **spec,
        },
    }


def test_loading_refuses_a_replica_count(tmp_path):
    with pytest.raises(ValueError, match="replicas"):
        Manifest.load(_manifest_file(tmp_path, _minimal(replicas=1)))


def test_loading_refuses_a_tagged_image(tmp_path):
    body = _minimal()
    body["spec"]["template"]["spec"]["containers"][0]["image"] = "img:latest"
    with pytest.raises(ValueError, match="tag"):
        Manifest.load(_manifest_file(tmp_path, body))


def test_loading_refuses_anything_but_a_deployment(tmp_path):
    body = _minimal()
    body["kind"] = "StatefulSet"
    with pytest.raises(ValueError, match="Deployment"):
        Manifest.load(_manifest_file(tmp_path, body))


def test_a_wake_stamps_the_deployment_not_its_pods():
    """On the Deployment's own metadata, so the sweeper can give a fresh wake
    time to reach its queue; not on the pod template, where a changing value
    would roll the pods on every wake."""
    from datetime import datetime, timezone

    from fishsense_services_orchestrator.nrp.manifests import WOKEN_AT, load_manifests

    woken = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
    manifest = next(iter(load_manifests(MANIFEST_DIR).values()))

    body = manifest.render(namespace="fishsense", image_tag="v1", replicas=1,
                           woken_at=woken)  # fmt: skip

    assert body["metadata"]["annotations"][WOKEN_AT] == woken.isoformat()
    template = body["spec"]["template"]["metadata"].get("annotations") or {}
    assert WOKEN_AT not in template


def test_the_leaf_a_wake_stamps_goes_on_the_pods():
    """Unlike the wake time, the Temporal leaf's fingerprint belongs on the pod
    template: it says which leaf the pods mounted, and it changes only when
    the cert sync pushes a new one -- exactly when the pods must roll. It is
    the key the sync compares (`ops.cert_sync`), and rendering is still pure."""
    from fishsense_services_orchestrator.nrp.manifests import (
        LEAF_SHA256,
        load_manifests,
    )
    from fishsense_services_orchestrator.ops import cert_sync

    manifest = next(iter(load_manifests(MANIFEST_DIR).values()))

    body = manifest.render(namespace="fishsense", image_tag="v1", replicas=1,
                           leaf_sha256="abc")  # fmt: skip
    unstamped = manifest.render(namespace="fishsense", image_tag="v1", replicas=1)

    assert LEAF_SHA256 == cert_sync.FINGERPRINT_ANNOTATION
    assert body["spec"]["template"]["metadata"]["annotations"][LEAF_SHA256] == "abc"
    assert LEAF_SHA256 not in (body["metadata"].get("annotations") or {})
    assert LEAF_SHA256 not in (
        unstamped["spec"]["template"]["metadata"].get("annotations") or {}
    )
    assert "annotations" not in manifest.body["spec"]["template"]["metadata"]
