"""Shared internals for standing the processor up on NRP and tearing it down.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/k8s_scaling.py.

The orchestrator is the only thing that knows when there's processor work to
do (it dispatches the child workflows), so it owns the processor's
Deployments: parent workflows stand one up before dispatching, and an hourly
sweeper tears it down when its task queue is quiet. This module centralizes
what both need -- the ``FISHSENSE_NRP_*`` settings, Kubernetes clients built
from the NRP kubeconfig, the wedge check, and the replica-target call.

**v2 stands the processor up and tears it down; it never scales it to zero**
(PLAN.md §3, decided 2026-09-27). NRP deletes Deployments older than two
weeks. v1's were applied once by hand and only ever had their replica counts
changed, so an idle one sat at zero until the rule took it -- around
2026-09-21, after which five days of stages silently didn't run. So a replica
target here is either a server-side apply of the whole manifest (create or
update, idempotent) or a delete; there is no Deployment at zero replicas.

Guardrails kept from v1, so "too many pods on NRP" can't happen by accident:

* Scaling is OFF unless ``FISHSENSE_NRP_KUBECONFIG_PATH`` is set -- locally and
  in e2e the processor runs under compose and these activities no-op.
* A replica target is absolute, never an increment -- N parents applying the
  same target converge on it; pods can't accumulate.
* Replica counts are clamped (``[1, MAX_ACTIVE_REPLICAS]``, the CPU fallback
  ``[1, MAX_FALLBACK_REPLICAS]``), so a misconfigured value can't ask NRP for
  an arbitrary count.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_orchestrator.nrp import manifests as kinds
from fishsense_services_orchestrator.nrp.gpu_fallback import FallbackPolicy, GpuState
from fishsense_services_orchestrator.nrp.manifests import (
    LEAF_SHA256,
    TEMPORAL_CERT_SECRET,
    Manifest,
    WOKEN_AT,
    load_manifests,
)

# Upper bound on any role's replica count. >1 is only ever a deliberate
# operator choice (a giant single dive, or resilience on a preemption-prone
# cluster); this caps the blast radius regardless of what is configured.
MAX_ACTIVE_REPLICAS: Final = 4
MIN_ACTIVE_REPLICAS: Final = 1

# The CPU fallback runs the same torch checkpoint without a GPU, so it is far
# slower per image and there is no point running several. Capped low
# independently of the GPU count.
MAX_FALLBACK_REPLICAS: Final = 2

#: Who owns the fields the orchestrator applies (server-side apply).
FIELD_MANAGER: Final = "fishsense-orchestrator"

#: Where the GPU fallback keeps its state (see `gpu_fallback`).
DEFAULT_STATE_CONFIG_MAP: Final = "fishsense-processor-gpu-fallback"

#: Where the orchestrator image carries deploy/nrp (see the Dockerfile).
DEFAULT_MANIFEST_DIR: Final = Path("/app/deploy/nrp")


class NrpSettings(BaseSettings):
    """``FISHSENSE_NRP_*``: v1's ``[kubernetes]`` section, plus the release's
    image tag and where the manifests are. Every knob is optional; only the
    kubeconfig turns scaling on, and then the namespace and tag are required
    (`resolve_scaling_config`)."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_NRP_")

    kubeconfig_path: str | None = None
    namespace: str | None = None
    #: The release's processor image tag, applied to every Deployment.
    image_tag: str | None = None
    manifest_dir: Path = DEFAULT_MANIFEST_DIR
    state_config_map: str = DEFAULT_STATE_CONFIG_MAP
    #: The Secret the pods mount their Temporal leaf from; `ops.cert_sync`
    #: reads the same setting.
    temporal_cert_secret: str = TEMPORAL_CERT_SECRET
    #: The per-image role's replicas: rawpy throughput on a memory-bound pod.
    active_replicas: int = 1
    light_active_replicas: int = 1
    idle_cooldown_minutes: int = 15
    #: How long a fresh wake is left alone by the sweeper, whatever its queue
    #: says: a parent stages raw frames (v1: up to an hour) before its child
    #: reaches the queue.
    wake_grace_minutes: int = 90
    gpu_active_replicas: int = 1
    gpu_fallback_replicas: int = 1
    gpu_start_timeout_seconds: int = 600
    gpu_wedge_grace_minutes: int = 5
    gpu_max_start_failures: int = 3
    gpu_fallback_minutes: int = 180


@dataclass(frozen=True)
class GpuScalingConfig:
    """Which Deployment serves the GPU queue, and when to stop waiting for it.

    ``deployment_name`` requests a GPU; ``fallback_deployment_name`` does not
    and runs the same torch checkpoint on the CPU. Exactly one is up at a time
    -- see `gpu_fallback`.
    """

    deployment_name: str = "fishsense-processor-gpu"
    fallback_deployment_name: str = "fishsense-processor-gpu-cpu-fallback"
    # How long to wait for a pod before calling a start attempt failed. This is
    # what separates "no GPU is available" from "the pod is still pulling its
    # image".
    start_timeout_seconds: int = 600
    policy: FallbackPolicy = field(default_factory=FallbackPolicy)


@dataclass(frozen=True)
class LightScalingConfig:
    """Which Deployment serves the light queue, and how many of it to run.

    Separate from `ScalingConfig.active_replicas` because the two knobs size
    against different limits. `active_replicas` buys rawpy throughput on a pod
    whose memory ceiling already forces a concurrency cap of 2; the light
    worker holds no image bytes, so it is bound by neither and one replica
    serves every stage on the queue (each drains one dive per firing).
    """

    deployment_name: str = "fishsense-processor-light"
    active_replicas: int = 1


@dataclass(frozen=True)
class ScalingConfig:
    """Resolved, validated ``FISHSENSE_NRP_*`` settings, and the manifests."""

    kubeconfig_path: str
    namespace: str
    image_tag: str
    #: Every processor Deployment's manifest, by Deployment name.
    manifests: Mapping[str, Manifest]
    per_image_deployment: str
    active_replicas: int
    idle_cooldown_minutes: int
    wake_grace_minutes: int = 90
    # Grouped rather than flattened: one subsystem each, which a caller that
    # only wants the per-image worker never touches.
    gpu: GpuScalingConfig = field(default_factory=GpuScalingConfig)
    light: LightScalingConfig = field(default_factory=LightScalingConfig)
    state_config_map: str = DEFAULT_STATE_CONFIG_MAP
    temporal_cert_secret: str = TEMPORAL_CERT_SECRET

    def manifest(self, name: str) -> Manifest:
        return self.manifests[name]

    def sweep_targets(self) -> tuple[tuple[str, str], ...]:
        """Every (Deployment, task queue) pair the idle sweeper must consider.

        Four Deployments, three queues: the per-image worker owns
        `fishsense_processor` and the light worker `fishsense_processor_light`,
        while the GPU worker and its CPU fallback both serve
        `fishsense_processor_gpu` (only one of them is up at a time).
        Returning the pairing here keeps the sweeper from having to know the
        topology.

        **A Deployment missing from this tuple is never torn down**, which on
        NRP means holding pods around the clock -- so adding a role means
        adding it here, not only to the wake path.
        """
        return (
            (self.per_image_deployment, PROCESSOR_TASK_QUEUE),
            (self.gpu.deployment_name, PROCESSOR_GPU_TASK_QUEUE),
            (self.gpu.fallback_deployment_name, PROCESSOR_GPU_TASK_QUEUE),
            (self.light.deployment_name, PROCESSOR_LIGHT_TASK_QUEUE),
        )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def kubeconfig_is_blank(path: str) -> bool:
    """A kubeconfig file that is there but empty: what an unseeded soft render
    leaves on the slot (vault-agent writes the file, blank, when
    `nrp_orchestrator` has no kubeconfig yet). It means "no NRP yet", like an
    unset path. A path to no file at all is a misconfiguration, left to fail."""
    file = Path(path)
    return file.is_file() and not file.read_text().strip()


def resolve_scaling_config(settings: NrpSettings | None = None) -> ScalingConfig | None:
    """Return the scaling config, or ``None`` when scaling is disabled.

    Disabled = no kubeconfig, or a blank one (the default -- the processor then
    runs under compose, always on). When there is one, the namespace and the image tag
    are required, the manifests must load, and every replica count is clamped.
    """
    settings = settings or NrpSettings()
    if not settings.kubeconfig_path or kubeconfig_is_blank(settings.kubeconfig_path):
        return None
    if not settings.namespace:
        raise ValueError(
            "FISHSENSE_NRP_NAMESPACE is required when FISHSENSE_NRP_KUBECONFIG_PATH is set"
        )
    if not settings.image_tag:
        raise ValueError(
            "FISHSENSE_NRP_IMAGE_TAG (the release's processor image tag) is "
            "required when FISHSENSE_NRP_KUBECONFIG_PATH is set"
        )

    by_kind = load_manifests(settings.manifest_dir)

    # The grace window must not outlast the start timeout. The GPU wake waits
    # out `gpu_start_timeout_seconds` for a pod, then observes; if the grace
    # were longer, that observation would always land inside it, no failure
    # would ever be counted, and the CPU fallback could never trip -- the one
    # outcome this whole mechanism exists to prevent.
    gpu_start_timeout_seconds = max(0, int(settings.gpu_start_timeout_seconds))
    wedge_grace_seconds = min(
        max(0, int(settings.gpu_wedge_grace_minutes)) * 60,
        gpu_start_timeout_seconds,
    )
    fallback_policy = FallbackPolicy(
        # Deliberately NOT `active_replicas`. That sizes the per-image worker,
        # where more pods is simply more throughput; here each pod holds a GPU
        # on a contended shared cluster. At 2 the second pod sat Pending on
        # "Insufficient nvidia.com/gpu" (2026-08-20). Defaults to 1.
        active_replicas=_clamp(
            settings.gpu_active_replicas, MIN_ACTIVE_REPLICAS, MAX_ACTIVE_REPLICAS
        ),
        fallback_replicas=_clamp(
            settings.gpu_fallback_replicas, 1, MAX_FALLBACK_REPLICAS
        ),
        # At least 1, or the pipeline would drop to CPU inference on the very
        # first observation and never actually try the GPU.
        max_start_failures=max(1, int(settings.gpu_max_start_failures)),
        wedge_grace=timedelta(seconds=wedge_grace_seconds),
        fallback_window=timedelta(minutes=max(1, int(settings.gpu_fallback_minutes))),
    )
    return ScalingConfig(
        kubeconfig_path=settings.kubeconfig_path,
        namespace=settings.namespace,
        image_tag=settings.image_tag,
        manifests={m.name: m for m in by_kind.values()},
        per_image_deployment=by_kind[kinds.PER_IMAGE].name,
        active_replicas=_clamp(
            settings.active_replicas, MIN_ACTIVE_REPLICAS, MAX_ACTIVE_REPLICAS
        ),
        idle_cooldown_minutes=max(0, int(settings.idle_cooldown_minutes)),
        wake_grace_minutes=max(0, int(settings.wake_grace_minutes)),
        gpu=GpuScalingConfig(
            deployment_name=by_kind[kinds.GPU].name,
            fallback_deployment_name=by_kind[kinds.GPU_CPU_FALLBACK].name,
            start_timeout_seconds=gpu_start_timeout_seconds,
            policy=fallback_policy,
        ),
        # Independent of `active_replicas` for the same reason the GPU count
        # is: the light stages drain one dive per firing each, so a second pod
        # adds nothing, and NRP asks us to hold as little as possible.
        light=LightScalingConfig(
            deployment_name=by_kind[kinds.LIGHT].name,
            active_replicas=_clamp(
                settings.light_active_replicas, MIN_ACTIVE_REPLICAS, MAX_ACTIVE_REPLICAS
            ),
        ),
        state_config_map=settings.state_config_map,
        temporal_cert_secret=settings.temporal_cert_secret,
    )


# -- clients --------------------------------------------------------------------


@dataclass(frozen=True)
class Kubernetes:
    """The two API groups the NRP stage uses, over one connection: ``apps``
    for the Deployments, ``core`` for the GPU-fallback ConfigMap and the
    Temporal Secret's fingerprint."""

    apps: Any
    core: Any


def kubernetes_apis(kubeconfig_path: str) -> Kubernetes:
    """Clients bound to the NRP cluster in ``kubeconfig_path``.

    Uses an explicit ``Configuration`` so we don't mutate the kubernetes
    client's global default config (activities can run concurrently). Imports
    the kubernetes client lazily so importing this module -- which the worker
    does at startup to register the activities -- doesn't pull it in until
    scaling is actually used.
    """
    # pylint: disable=import-outside-toplevel
    from kubernetes import client as k8s_client, config as k8s_config

    configuration = k8s_client.Configuration()
    k8s_config.load_kube_config(
        config_file=kubeconfig_path, client_configuration=configuration
    )
    api_client = k8s_client.ApiClient(configuration)
    _relax_x509_strict_verification(api_client, configuration)
    return Kubernetes(
        apps=k8s_client.AppsV1Api(api_client), core=k8s_client.CoreV1Api(api_client)
    )


def _apply_relaxed_verification(ctx: ssl.SSLContext) -> None:
    """Clear OpenSSL 3.x strict mode on ``ctx`` — nothing else.

    Verification stays fully on (``CERT_REQUIRED`` + hostname check); we only
    drop the ``VERIFY_X509_STRICT`` flag that Python 3.13 enabled by default.
    Kept separate so the security-critical invariant is unit-testable.
    """
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT


def _relax_x509_strict_verification(api_client, configuration) -> None:
    """Verify NRP's apiserver cert fully, minus OpenSSL 3.x strict mode.

    Python 3.13 turned on ``ssl.VERIFY_X509_STRICT`` by default, which enforces
    RFC 5280 to the letter — including a mandatory Authority Key Identifier on
    leaf certs. NRP/Nautilus's kubeadm-generated kube-apiserver cert omits AKI,
    so the (otherwise valid) cert is rejected with "Missing Authority Key
    Identifier" and every call fails the TLS handshake.

    The kubernetes client's ``Configuration`` exposes no ``ssl_context``; it
    builds its own strict urllib3 context from ``ca_certs``/``cert_reqs``. We
    swap in a context that keeps full verification — ``CERT_REQUIRED`` against
    the pinned cluster CA plus hostname/IP checking — and clears ONLY the
    strict flag. This is emphatically NOT ``insecure-skip-tls-verify``: it
    restores the verification level Python used by default through 3.12.

    No-op when the kubeconfig disables verification (``verify_ssl`` False) —
    there's nothing to relax and we must not silently re-enable it.
    """
    if not (configuration.verify_ssl and configuration.ssl_ca_cert):
        return
    ctx = ssl.create_default_context(cafile=configuration.ssl_ca_cert)
    _apply_relaxed_verification(ctx)
    # Client-cert kubeconfigs (not our token-based one, but stay correct):
    # the context now owns the whole client-side of the handshake.
    if configuration.cert_file and configuration.key_file:
        ctx.load_cert_chain(configuration.cert_file, configuration.key_file)
    # urllib3 gets ambiguous if both ssl_context and ca_certs/cert_reqs are
    # set — hand verification entirely to our context.
    pool_kw = api_client.rest_client.pool_manager.connection_pool_kw
    pool_kw["ssl_context"] = ctx
    for key in ("ca_certs", "cert_reqs", "cert_file", "key_file"):
        pool_kw.pop(key, None)


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404


# -- Deployments ----------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    """What one Deployment read says about whether its pods are serving.

    ``ready`` and ``wedged`` are not complements: a Deployment that doesn't
    exist (v2's idle state; v1's was zero replicas) is neither.
    `gpu_fallback.decide` depends on telling that third case apart -- counting
    a torn-down Deployment as a failed start would trip the CPU fallback during
    normal idle operation.
    """

    desired: int
    ready_count: int

    @property
    def ready(self) -> bool:
        """At least one pod is Ready, so the queue can drain."""
        return self.ready_count > 0

    @property
    def wedged(self) -> bool:
        """Pods are wanted and none is Ready."""
        return self.desired > 0 and self.ready_count == 0


def readiness(deployment) -> Readiness:
    """Read `Readiness` off a Deployment object, or ``None`` for one that
    doesn't exist."""
    if deployment is None:
        return Readiness(desired=0, ready_count=0)
    desired = (deployment.spec.replicas if deployment.spec else None) or 0
    status = deployment.status
    # k8s OMITS readyReplicas rather than sending 0, so this is None in exactly
    # the wedged case. Reading None as "unknown, assume healthy" is what would
    # keep the GPUs pinned.
    ready_count = (status.ready_replicas if status else None) or 0
    return Readiness(desired=desired, ready_count=ready_count)


def read_deployment(apps, namespace: str, name: str):
    """The Deployment, or ``None`` when it doesn't exist."""
    try:
        return apps.read_namespaced_deployment(name=name, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return None
        raise


def deployment_is_wedged(apps, namespace: str, name: str) -> bool:
    """True iff the Deployment wants pods but has no Ready one.

    This is the escape hatch on the sweeper's "is the queue busy?" check, and
    it exists because those two signals can disagree in exactly one direction
    that matters. A processor that cannot start — expired Temporal cert, bad
    image tag, unschedulable GPU request, exhausted quota — never drains its
    task queue, so every dispatched child sits ``Running`` until it times out
    and the queue never *looks* idle. The sweeper then declines to tear down,
    and the Deployment holds NRP GPUs around the clock while getting no work
    done. v1's prod sat exactly there from 2026-08-14: the failure suppressed
    the very cleanup that would have bounded its cost.

    "Busy" is only a reason to keep pods alive when the pods can make progress.

    Deliberately shallow — ``spec.replicas`` versus ``status.readyReplicas``,
    both from one Deployment read (the deploy identity has no ``pods``
    access). Richer signals were measured against the live wedge and rejected:

    * ``Progressing`` read ``True``/``NewReplicaSetAvailable`` throughout — the
      ReplicaSet *had* progressed, back when a pod first came up.
    * ``Available``'s ``lastTransitionTime`` is useless as a "wedged since"
      clock. The pod has no readinessProbe, so each crash cycle flips it Ready
      then not-Ready and the timestamp resets every few seconds.

    Nothing here is time-based, which leaves one false positive: a sweep that
    lands inside a genuine cold start (image pull) sees no Ready pod. The
    sweeper covers that with the wake stamp: for the start timeout after a
    wake, busy with no Ready pod is a cold start, not a wedge.

    A Deployment that doesn't exist is not wedged (v2): nothing is held.
    """
    return readiness(read_deployment(apps, namespace, name)).wedged


def apply_deployment(apps, body: dict) -> None:
    """Server-side apply ``body``: create the Deployment, or update it to match.

    Idempotent, and the reason v2 survives NRP's two-week rule: whether the
    Deployment is there, gone, or drifted, one call converges it. ``force``
    takes the fields from any other manager (an operator's `kubectl edit`),
    since the orchestrator owns these Deployments outright; applying an
    unchanged body is a server-side no-op that rolls no pods.
    """
    apps.patch_namespaced_deployment(
        name=body["metadata"]["name"],
        namespace=body["metadata"]["namespace"],
        body=body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


def woken_at(apps, namespace: str, name: str) -> datetime | None:
    """When the orchestrator last woke Deployment ``name`` (its `WOKEN_AT`
    stamp). None when it doesn't exist, isn't stamped, or the stamp is
    unreadable."""
    try:
        deployment = apps.read_namespaced_deployment(name=name, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return None
        raise
    annotations = getattr(getattr(deployment, "metadata", None), "annotations", None)
    stamp = (annotations or {}).get(WOKEN_AT)
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


# -- who woke a Deployment ----------------------------------------------------------
#
# `WOKEN_AT` says when, not for whom, so "work has reached the queue since the
# wake" can't tell one parent's child from another's: a second parent's short
# child ended the first parent's wake while it was still staging. Each wake
# therefore also records its parent workflow, one annotation per parent, and
# the sweeper leaves the Deployment while any of them is still running.
#
# Written by JSON merge patch under their own field manager, not in the
# applied body: a merge patch sets only its key (concurrent wakes by different
# parents all land, with no read-modify-write), and the orchestrator's next
# server-side apply, which doesn't list them, leaves another manager's fields.

#: Field manager of the per-parent wake records (never `FIELD_MANAGER`).
WAKE_FIELD_MANAGER: Final = "fishsense-orchestrator-wake"
#: Annotation-key prefix of a wake record; the name part hashes the workflow id.
WAKER_PREFIX: Final = "woken-by.fishsense.e4e/"


@dataclass(frozen=True)
class Waker:
    """A parent workflow that woke a Deployment, and when."""

    workflow_id: str
    run_id: str
    at: datetime


def waker_annotation(waker: Waker) -> tuple[str, str]:
    """The (key, value) recording ``waker``: one key per parent workflow, so a
    parent's later wake replaces its own record and no one else's."""
    digest = hashlib.sha256(waker.workflow_id.encode()).hexdigest()[:32]
    value = json.dumps(
        {
            "workflow_id": waker.workflow_id,
            "run_id": waker.run_id,
            "at": waker.at.isoformat(),
        }
    )
    return f"{WAKER_PREFIX}{digest}", value


def _wakers(annotations: Mapping[str, str] | None) -> dict[str, Waker]:
    found = {}
    for key, value in (annotations or {}).items():
        if not key.startswith(WAKER_PREFIX):
            continue
        try:
            record = json.loads(value)
            found[key] = Waker(
                record["workflow_id"],
                record["run_id"],
                datetime.fromisoformat(record["at"]),
            )
        except (ValueError, KeyError, TypeError):
            continue  # unreadable: protects nothing
    return found


def read_wakers(apps, namespace: str, name: str) -> list[Waker]:
    """Every parent recorded as having woken Deployment ``name``, oldest
    first; none when it doesn't exist."""
    return sorted(
        _read_waker_records(apps, namespace, name).values(), key=lambda w: w.at
    )


def _read_waker_records(apps, namespace: str, name: str) -> dict[str, Waker]:
    try:
        deployment = apps.read_namespaced_deployment(name=name, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return {}
        raise
    metadata = getattr(deployment, "metadata", None)
    return _wakers(getattr(metadata, "annotations", None))


def _patch_annotations(apps, namespace: str, name: str, changes: dict) -> None:
    apps.patch_namespaced_deployment(
        name=name,
        namespace=namespace,
        body={"metadata": {"annotations": changes}},
        field_manager=WAKE_FIELD_MANAGER,
        _content_type="application/merge-patch+json",
    )


def record_waker(apps, namespace: str, name: str, waker: Waker) -> None:
    """Record that ``waker`` woke Deployment ``name`` (just applied). Nothing
    to record on a Deployment that isn't there."""
    key, value = waker_annotation(waker)
    try:
        _patch_annotations(apps, namespace, name, {key: value})
    except Exception as exc:  # pylint: disable=broad-except
        if not _is_not_found(exc):
            raise


def forget_wakers_before(
    apps, namespace: str, name: str, cutoff: datetime
) -> list[Waker]:
    """Drop the records older than ``cutoff`` (they protect nothing any
    more); return the rest. A Deployment that's gone has none."""
    records = _read_waker_records(apps, namespace, name)
    stale = {key: None for key, w in records.items() if w.at < cutoff}
    if stale:
        try:
            _patch_annotations(apps, namespace, name, stale)
        except Exception as exc:  # pylint: disable=broad-except
            if not _is_not_found(exc):
                raise
    return [w for key, w in records.items() if key not in stale]


def delete_deployment(apps, namespace: str, name: str) -> bool:
    """Delete the Deployment and its pods. False when it was already gone."""
    try:
        apps.delete_namespaced_deployment(
            name=name, namespace=namespace, propagation_policy="Background"
        )
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return False
        raise
    return True


def current_leaf(core, config: ScalingConfig) -> str | None:
    """The Temporal leaf the processor's Secret holds now (the fingerprint
    `ops.cert_sync` records on it), or None before the first sync."""
    try:
        secret = core.read_namespaced_secret(
            name=config.temporal_cert_secret, namespace=config.namespace
        )
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return None
        raise
    annotations = getattr(getattr(secret, "metadata", None), "annotations", None)
    return (annotations or {}).get(LEAF_SHA256)


def set_deployment_replicas(
    apps,
    config: ScalingConfig,
    name: str,
    replicas: int,
    *,
    leaf_sha256: str | None = None,
) -> None:
    """Bring Deployment ``name`` to an absolute replica target.

    v1 patched the scale subresource; v2 stands the Deployment up (a
    server-side apply of its manifest at ``replicas``, with the release's
    image) or, for a target of zero, tears it down. Idempotent either way --
    the target is absolute, never an increment, and it always issues the call
    rather than reading first to skip it.

    ``leaf_sha256`` (`current_leaf`) records which Temporal leaf the pods
    mount, so `ops.cert_sync` rolls this Deployment only once that leaf is
    replaced.
    """
    if replicas > 0:
        apply_deployment(
            apps,
            config.manifest(name).render(
                namespace=config.namespace,
                image_tag=config.image_tag,
                replicas=replicas,
                woken_at=datetime.now(timezone.utc),
                leaf_sha256=leaf_sha256,
            ),
        )
    else:
        delete_deployment(apps, config.namespace, name)


# -- the GPU fallback's state ------------------------------------------------------


def read_gpu_state(core, namespace: str, name: str) -> GpuState:
    """The GPU fallback's state from its ConfigMap; no ConfigMap is no history."""
    try:
        config_map = core.read_namespaced_config_map(name=name, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return GpuState()
        raise
    return GpuState.from_config_map(getattr(config_map, "data", None))


def write_gpu_state(core, namespace: str, name: str, state: GpuState) -> None:
    """Merge ``state`` into its ConfigMap, creating it on first write.

    A strategic-merge patch of ``data`` only, where ``None`` removes a key, so
    a healthy state leaves the ConfigMap empty rather than full of zeroes.
    """
    data = state.to_config_map()
    try:
        core.patch_namespaced_config_map(
            name=name, namespace=namespace, body={"data": data}
        )
    except Exception as exc:  # pylint: disable=broad-except
        if not _is_not_found(exc):
            raise
        try:
            core.create_namespaced_config_map(
                namespace=namespace,
                body={
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": name,
                        "labels": {"app.kubernetes.io/part-of": "fishsense"},
                    },
                    "data": {k: v for k, v in data.items() if v is not None},
                },
            )
        except Exception as race:  # pylint: disable=broad-except
            # Another wake created it between our patch and our create.
            if getattr(race, "status", None) != 409:
                raise
            core.patch_namespaced_config_map(
                name=name, namespace=namespace, body={"data": data}
            )
