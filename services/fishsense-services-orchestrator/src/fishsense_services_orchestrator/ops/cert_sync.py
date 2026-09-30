"""Mirror the slot's rotated Temporal leaf into the processor's NRP Secret.

Ported from fishsense-lite@77e8f8e5 deploy/incus/nrp_cert_sync/sync.sh.

    python -m fishsense_services_orchestrator.ops.cert_sync

**Why it exists.** The krg-prod Temporal mTLS leaf is a 7-day cert. On the
slot, vault-agent re-renders it before expiry, and the `temporal.reload` hook
restarts the services that hold it (a process builds its TLS config once, at
connect). The processor runs on NRP, out of vault-agent's reach, and holds the
same identity (CN=fishsense-worker) in a Kubernetes Secret. In v1 nothing
renewed that copy: it expired on 2026-08-14 and every pod crash-looped on
`CertificateExpired`. The slot is where the renewed leaf lands, so the slot
pushes it on: run this one-shot after every rotation (list it in
`temporal.reload`) and on every converge. It is a no-op when the leaf is
unchanged, recorded by a sha256 annotation on the Secret.

**Then it rolls the processor.** kubelet refreshes a mounted Secret in place,
but the processor reads /certs once, at connect, so a running pod keeps the
old leaf until it is replaced -- and a Deployment busy across a rotation is
Ready with a busy queue, so the sweeper never tears it down: it would hold the
old leaf past expiry, the 2026-08-14 outage again. So, as v1 did, every
processor Deployment that exists is rolled onto the new leaf, by a pod-template
annotation (`LEAF_SHA256`, the leaf's sha256). One that doesn't exist is
skipped: it is stood up on demand, and its next wake's pods mount the new
Secret.

v1's rules, kept: an absent NRP kubeconfig (a soft render: not seeded yet, or a
slot without NRP) is a clean no-op, exit 0 -- and so is an empty one, which is
what an unseeded soft render actually leaves on the slot; a missing cert render is a hard
error, exit 1; the Secret is upserted (created when missing, e.g. wiped with
the namespace) under the keys the processor mounts: ``client.pem``,
``client.key``, ``root-ca.pem``.

v2 changes:

* **the roll is decided per Deployment, from the leaf its pods mount.** v1
  ran `kubectl rollout restart` on every Deployment after a push. v2 patches
  the leaf's sha256 onto each Deployment's pod template, and a wake stamps the
  same annotation, from the Secret, on the Deployments it stands up
  (`nrp.scaling.current_leaf`). So a Deployment whose record is already the
  current leaf -- stood up after the push -- isn't restarted for nothing, and a
  roll that failed is retried by the next run: v1 annotated the Secret first,
  so after a failed roll every later run saw an unchanged leaf and did nothing;
* the Deployments rolled are the manifests' (``deploy/nrp``,
  ``FISHSENSE_NRP_MANIFEST_DIR``), where v1 listed its names in the script;
* the keys and the fingerprint are written in one request (v1 applied, then
  annotated);
* Python on the orchestrator's image, with the NRP stage's kubeconfig handling
  (``FISHSENSE_NRP_KUBECONFIG_PATH``/``_NAMESPACE``, and its TLS relaxation for
  NRP's apiserver cert), where v1 was a shell script in an `alpine/kubectl`
  container. The leaf is the orchestrator's own Temporal client cert
  (``FISHSENSE_TEMPORAL_CLIENT_CERT`` etc.): the same vault-agent render.
  The orchestrator's NRP Role grants exactly this Secret, and the `get` and
  `patch` on Deployments the wakes already hold (deploy/nrp/
  deployer-rbac.yaml).
"""

from __future__ import annotations

import base64
import enum
import hashlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_orchestrator.nrp.manifests import (
    LEAF_SHA256,
    TEMPORAL_CERT_SECRET,
    load_manifests,
)
from fishsense_services_orchestrator.nrp.scaling import (
    DEFAULT_MANIFEST_DIR,
    Kubernetes,
    kubernetes_apis,
)

__all__ = [
    "CA_KEY",
    "CERT_KEY",
    "CertSyncSettings",
    "DEFAULT_SECRET_NAME",
    "FIELD_MANAGER",
    "KEY_KEY",
    "Outcome",
    "main",
    "processor_deployments",
    "sync",
]

log = logging.getLogger("nrp-temporal-cert-sync")

#: The Secret every processor Deployment mounts at /certs (deploy/nrp). The
#: name is v1's, so v1's and v2's processors share one forwarded leaf.
DEFAULT_SECRET_NAME: Final = TEMPORAL_CERT_SECRET
#: Which leaf the Secret holds, so a converge that changed nothing writes
#: nothing; and, on a pod template, which leaf those pods mount.
FINGERPRINT_ANNOTATION: Final = LEAF_SHA256
#: Who owns the roll's pod-template annotation. Not the orchestrator's
#: manager: its server-side apply would then drop the annotation from a
#: Deployment whose wake had no leaf to stamp, rolling the pods again.
FIELD_MANAGER: Final = "fishsense-cert-sync"
CERT_KEY: Final = "client.pem"
KEY_KEY: Final = "client.key"
CA_KEY: Final = "root-ca.pem"


class _NrpEnv(BaseSettings):
    """``FISHSENSE_NRP_*``, as the NRP stage reads it."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_NRP_", extra="ignore")

    kubeconfig_path: str | None = None
    namespace: str | None = None
    temporal_cert_secret: str = DEFAULT_SECRET_NAME
    manifest_dir: Path = DEFAULT_MANIFEST_DIR


class _TemporalEnv(BaseSettings):
    """The leaf's paths, as the orchestrator's Temporal connection reads them."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_TEMPORAL_", extra="ignore")

    client_cert: Path | None = None
    client_private_key: Path | None = None
    server_root_ca_cert: Path | None = None


class CertSyncSettings(BaseModel):
    """Where the leaf is, and which cluster and Secret it goes to."""

    #: Unset, NRP is off, and there is nothing to sync.
    kubeconfig_path: str | None = None
    namespace: str | None = None
    secret_name: str = DEFAULT_SECRET_NAME
    client_cert: Path | None = None
    client_private_key: Path | None = None
    server_root_ca_cert: Path | None = None
    #: The processor Deployments' manifests: the ones rolled onto a new leaf.
    manifest_dir: Path = DEFAULT_MANIFEST_DIR

    @classmethod
    def from_env(cls) -> "CertSyncSettings":
        """From the orchestrator's own ``FISHSENSE_NRP_*`` and
        ``FISHSENSE_TEMPORAL_*``: the same cluster and the same leaf."""
        nrp, temporal = _NrpEnv(), _TemporalEnv()
        return cls(
            kubeconfig_path=nrp.kubeconfig_path,
            namespace=nrp.namespace,
            secret_name=nrp.temporal_cert_secret,
            manifest_dir=nrp.manifest_dir,
            client_cert=temporal.client_cert,
            client_private_key=temporal.client_private_key,
            server_root_ca_cert=temporal.server_root_ca_cert,
        )


class Outcome(enum.Enum):
    NO_CLUSTER = "no kubeconfig: nothing to sync"
    UNCHANGED = "leaf unchanged: no update"
    PUSHED = "pushed the rotated leaf"


class MissingCertRender(RuntimeError):
    """The leaf to forward isn't there: the slot's Temporal render is off."""


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404


def _leaf_files(settings: CertSyncSettings) -> dict[str, Path]:
    files = {
        CERT_KEY: settings.client_cert,
        KEY_KEY: settings.client_private_key,
        CA_KEY: settings.server_root_ca_cert,
    }
    for key, path in files.items():
        if path is None or not Path(path).is_file():
            raise MissingCertRender(
                f"missing {path or key} -- is the slot's Temporal render enabled?"
            )
    return {key: Path(path) for key, path in files.items()}


def _current_fingerprint(core: Any, namespace: str, name: str) -> str | None:
    """The fingerprint the Secret records; None when it has none, or there is
    no Secret (first run, or one wiped with the namespace)."""
    try:
        secret = core.read_namespaced_secret(name=name, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return None
        raise
    annotations = getattr(secret.metadata, "annotations", None) or {}
    return annotations.get(FINGERPRINT_ANNOTATION)


def _upsert(core: Any, namespace: str, name: str, data: dict, fingerprint: str) -> None:
    """Replace the three keys and the fingerprint in one request; create the
    Secret when there is none."""
    metadata = {"annotations": {FINGERPRINT_ANNOTATION: fingerprint}}
    try:
        core.patch_namespaced_secret(
            name=name, namespace=namespace, body={"metadata": metadata, "data": data}
        )
    except Exception as exc:  # pylint: disable=broad-except
        if not _is_not_found(exc):
            raise
        core.create_namespaced_secret(
            namespace=namespace,
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": name,
                    "labels": {"app.kubernetes.io/part-of": "fishsense"},
                    **metadata,
                },
                "data": data,
            },
        )


def processor_deployments(settings: CertSyncSettings) -> list[str]:
    """Every processor Deployment, by its manifest's name: each mounts the
    Secret (a test pins that), so each is rolled onto a new leaf."""
    return [m.name for m in load_manifests(Path(settings.manifest_dir)).values()]


def _pods_leaf(apps: Any, namespace: str, name: str) -> tuple[bool, str | None]:
    """Whether Deployment ``name`` exists, and the leaf its pod template
    records (None when it records none)."""
    try:
        deployment = apps.read_namespaced_deployment(name=name, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return False, None
        raise
    template = getattr(getattr(deployment, "spec", None), "template", None)
    annotations = getattr(getattr(template, "metadata", None), "annotations", None)
    return True, (annotations or {}).get(FINGERPRINT_ANNOTATION)


def _roll(apps: Any, namespace: str, names: list[str], fingerprint: str) -> None:
    """Roll every existing Deployment in ``names`` whose pods didn't start on
    ``fingerprint``, by setting it on the pod template -- as `kubectl rollout
    restart` does with its own annotation."""
    body = {
        "spec": {
            "template": {"metadata": {"annotations": {FINGERPRINT_ANNOTATION: fingerprint}}}  # fmt: skip
        }
    }
    for name in names:
        exists, leaf = _pods_leaf(apps, namespace, name)
        if not exists:
            log.info("%s not up - skipping (its next wake mounts the leaf)", name)
            continue
        if leaf == fingerprint:
            continue
        log.info("rolling %s onto the new leaf", name)
        apps.patch_namespaced_deployment(
            name=name, namespace=namespace, body=body, field_manager=FIELD_MANAGER
        )


def _has_kubeconfig(path: str | None) -> bool:
    """A kubeconfig to use: set, a file, and not blank. Blank is what an unseeded
    soft render leaves on the slot (vault-agent writes the file, empty, when a
    `{{ with secret }}` finds nothing), so it means "not seeded", like absent."""
    if not path or not Path(path).is_file():
        return False
    return bool(Path(path).read_text().strip())


def sync(
    settings: CertSyncSettings,
    *,
    kubernetes: Callable[[str], Kubernetes] = kubernetes_apis,
) -> Outcome:
    """Push the leaf to the Secret if it isn't already there, then roll every
    processor Deployment whose pods don't have it."""
    kubeconfig = settings.kubeconfig_path
    if not _has_kubeconfig(kubeconfig):
        log.info("no kubeconfig at %s - nothing to sync", kubeconfig)
        return Outcome.NO_CLUSTER
    if not settings.namespace:
        raise ValueError("FISHSENSE_NRP_NAMESPACE is required with a kubeconfig")

    files = _leaf_files(settings)
    # Before any write, so a bad manifest directory changes nothing.
    deployments = processor_deployments(settings)
    fingerprint = hashlib.sha256(files[CERT_KEY].read_bytes()).hexdigest()
    apis = kubernetes(kubeconfig)
    namespace, name = settings.namespace, settings.secret_name

    if _current_fingerprint(apis.core, namespace, name) == fingerprint:
        log.info("leaf unchanged (%s) - no update", fingerprint)
        outcome = Outcome.UNCHANGED
    else:
        log.info("pushing the rotated leaf to %s/%s", namespace, name)
        data = {
            key: base64.b64encode(path.read_bytes()).decode("ascii")
            for key, path in files.items()
        }
        _upsert(apis.core, namespace, name, data, fingerprint)
        outcome = Outcome.PUSHED
    # On an unchanged leaf too: that is how a roll that failed last run is
    # finished. Each Deployment already on the leaf is left alone.
    _roll(apis.apps, namespace, deployments, fingerprint)
    log.info("done (%s)", fingerprint)
    return outcome


def main(
    settings: CertSyncSettings | None = None,
    *,
    kubernetes: Callable[[str], Kubernetes] = kubernetes_apis,
) -> int:
    """The one-shot's exit code: 0 when synced or nothing to do, 1 when the
    leaf to forward is missing."""
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        sync(settings or CertSyncSettings.from_env(), kubernetes=kubernetes)
    except MissingCertRender as exc:
        log.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
