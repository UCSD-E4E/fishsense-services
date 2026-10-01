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

v1's rules, kept: an absent NRP kubeconfig (a soft render: not seeded yet, or a
slot without NRP) is a clean no-op, exit 0; a missing cert render is a hard
error, exit 1; the Secret is upserted (created when missing, e.g. wiped with
the namespace) under the keys the processor mounts: ``client.pem``,
``client.key``, ``root-ca.pem``.

v2 changes:

* **no rollout restart.** v1 rolled every Deployment mounting the Secret,
  because its Deployments lived forever and a scale-up reused their pods'
  template. v2 stands the processor up per wake and tears it down when idle
  (PLAN.md §3), so the next wake's pods mount the current Secret; and a pod that
  outlives a rotation reads the new leaf when its container restarts, since
  kubelet refreshes a mounted Secret in place;
* the keys and the fingerprint are written in one request (v1 applied, then
  annotated);
* Python on the orchestrator's image, with the NRP stage's kubeconfig handling
  (``FISHSENSE_NRP_KUBECONFIG_PATH``/``_NAMESPACE``, and its TLS relaxation for
  NRP's apiserver cert), where v1 was a shell script in an `alpine/kubectl`
  container. The leaf is the orchestrator's own Temporal client cert
  (``FISHSENSE_TEMPORAL_CLIENT_CERT`` etc.): the same vault-agent render.
  The orchestrator's NRP Role grants exactly this Secret (deploy/nrp/
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

__all__ = [
    "CA_KEY",
    "CERT_KEY",
    "CertSyncSettings",
    "DEFAULT_SECRET_NAME",
    "KEY_KEY",
    "Outcome",
    "main",
    "sync",
]

log = logging.getLogger("nrp-temporal-cert-sync")

#: The Secret every processor Deployment mounts at /certs (deploy/nrp). The
#: name is v1's, so v1's and v2's processors share one forwarded leaf.
DEFAULT_SECRET_NAME: Final = "fishsense-data-worker-temporal-certs"
#: Which leaf the Secret holds, so a converge that changed nothing writes
#: nothing.
FINGERPRINT_ANNOTATION: Final = "fishsense.e4e.ucsd.edu/leaf-sha256"
CERT_KEY: Final = "client.pem"
KEY_KEY: Final = "client.key"
CA_KEY: Final = "root-ca.pem"


class _NrpEnv(BaseSettings):
    """``FISHSENSE_NRP_*``, as the NRP stage reads it."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_NRP_", extra="ignore")

    kubeconfig_path: str | None = None
    namespace: str | None = None
    temporal_cert_secret: str = DEFAULT_SECRET_NAME


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

    @classmethod
    def from_env(cls) -> "CertSyncSettings":
        """From the orchestrator's own ``FISHSENSE_NRP_*`` and
        ``FISHSENSE_TEMPORAL_*``: the same cluster and the same leaf."""
        nrp, temporal = _NrpEnv(), _TemporalEnv()
        return cls(
            kubeconfig_path=nrp.kubeconfig_path,
            namespace=nrp.namespace,
            secret_name=nrp.temporal_cert_secret,
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


def _core_api(kubeconfig_path: str) -> Any:
    # pylint: disable=import-outside-toplevel
    from fishsense_services_orchestrator.nrp.scaling import kubernetes_apis

    return kubernetes_apis(kubeconfig_path).core


def sync(
    settings: CertSyncSettings,
    *,
    core_factory: Callable[[str], Any] = _core_api,
) -> Outcome:
    """Push the leaf to the Secret if it isn't already there."""
    kubeconfig = settings.kubeconfig_path
    if not kubeconfig or not Path(kubeconfig).is_file():
        log.info("no kubeconfig at %s - nothing to sync", kubeconfig)
        return Outcome.NO_CLUSTER
    if not settings.namespace:
        raise ValueError("FISHSENSE_NRP_NAMESPACE is required with a kubeconfig")

    files = _leaf_files(settings)
    fingerprint = hashlib.sha256(files[CERT_KEY].read_bytes()).hexdigest()
    core = core_factory(kubeconfig)
    namespace, name = settings.namespace, settings.secret_name

    if _current_fingerprint(core, namespace, name) == fingerprint:
        log.info("leaf unchanged (%s) - no update", fingerprint)
        return Outcome.UNCHANGED

    log.info("pushing the rotated leaf to %s/%s", namespace, name)
    data = {
        key: base64.b64encode(path.read_bytes()).decode("ascii")
        for key, path in files.items()
    }
    _upsert(core, namespace, name, data, fingerprint)
    log.info("done (%s)", fingerprint)
    return Outcome.PUSHED


def main(
    settings: CertSyncSettings | None = None,
    *,
    core_factory: Callable[[str], Any] = _core_api,
) -> int:
    """The one-shot's exit code: 0 when synced or nothing to do, 1 when the
    leaf to forward is missing."""
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        sync(settings or CertSyncSettings.from_env(), core_factory=core_factory)
    except MissingCertRender as exc:
        log.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
