"""The processor's Deployment manifests, as the orchestrator applies them.

v1 (fishsense-lite@77e8f8e5 deploy/k8s/data-worker) applied its four
Deployments once with `kubectl apply -k`, kustomize setting the image tag, and
from then on only changed their replica counts. NRP deletes Deployments older
than two weeks, so around 2026-09-21 they were gone, and five days of stages
silently didn't run (PLAN.md §3).

v2's orchestrator applies them itself, on every wake: it reads the files in
``deploy/nrp`` (one per Deployment), sets the namespace, the replica target
and the release's image tag, and server-side applies the result. So the files
leave out what the orchestrator owns -- ``spec.replicas`` and the image tag --
and `Manifest.load` refuses a file that sets either, since a value there would
either fight the orchestrator or be silently overwritten by it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "GPU",
    "GPU_CPU_FALLBACK",
    "LIGHT",
    "MANIFEST_FILES",
    "PER_IMAGE",
    "Manifest",
    "load_manifests",
]

PER_IMAGE: Final = "per_image"
LIGHT: Final = "light"
GPU: Final = "gpu"
GPU_CPU_FALLBACK: Final = "gpu_cpu_fallback"

#: One file per processor Deployment, in ``deploy/nrp``.
MANIFEST_FILES: Final[dict[str, str]] = {
    PER_IMAGE: "per-image.yaml",
    LIGHT: "light.yaml",
    GPU: "gpu.yaml",
    GPU_CPU_FALLBACK: "gpu-cpu-fallback.yaml",
}


def _containers(body: dict[str, Any]) -> list[dict[str, Any]]:
    return body["spec"]["template"]["spec"]["containers"]


def _has_tag(image: str) -> bool:
    # A registry host may carry a port (`host:5000/img`), so only a colon in
    # the last path segment is a tag. A digest pins an image as firmly.
    return ":" in image.rsplit("/", 1)[-1] or "@" in image


#: When the orchestrator last stood a Deployment up. A parent wakes a
#: processor, then stages raw frames for a while before its child reaches the
#: queue; a sweep in between saw a quiet queue and deleted the fresh
#: Deployment, leaving the child on an unserved queue (v1 had the same race).
WOKEN_AT: Final = "fishsense.e4e/woken-at"


@dataclass(frozen=True)
class Manifest:
    """One Deployment, as committed."""

    body: dict[str, Any]

    @property
    def name(self) -> str:
        return self.body["metadata"]["name"]

    @classmethod
    def load(cls, path: Path) -> Manifest:
        body = yaml.safe_load(path.read_text())
        if not isinstance(body, dict) or body.get("kind") != "Deployment":
            raise ValueError(f"{path}: expected one apps/v1 Deployment")
        if "replicas" in body.get("spec", {}):
            raise ValueError(f"{path}: sets spec.replicas, which the orchestrator owns")
        for container in _containers(body):
            if _has_tag(container["image"]):
                raise ValueError(
                    f"{path}: image {container['image']!r} has a tag; the "
                    "orchestrator sets the release's"
                )
        return cls(body)

    def render(
        self,
        *,
        namespace: str,
        image_tag: str,
        replicas: int,
        woken_at: datetime | None = None,
    ) -> dict:
        """The body to apply: the file, in ``namespace``, at ``replicas``,
        every container on ``image_tag``. Pure, so the same inputs give the
        same body, and re-applying it rolls no pods.

        ``woken_at`` stamps the **Deployment's** metadata, so the sweeper can
        give a fresh wake time to reach its queue (`WOKEN_AT`). Never the pod
        template's, where a changing value would roll the pods on every wake.
        """
        body = copy.deepcopy(self.body)
        body["metadata"]["namespace"] = namespace
        if woken_at is not None:
            annotations = body["metadata"].setdefault("annotations", {})
            annotations[WOKEN_AT] = woken_at.isoformat()
        body["spec"]["replicas"] = replicas
        for container in _containers(body):
            container["image"] = f"{container['image']}:{image_tag}"
        return body


def load_manifests(directory: Path) -> dict[str, Manifest]:
    """Every processor Deployment's manifest, by kind. A missing file raises
    here -- the orchestrator's startup -- rather than at the first wake."""
    return {kind: Manifest.load(directory / f) for kind, f in MANIFEST_FILES.items()}
