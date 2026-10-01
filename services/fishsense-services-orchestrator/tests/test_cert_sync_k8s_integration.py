"""The Temporal cert sync against a real Kubernetes apiserver.

Ported from fishsense-lite@77e8f8e5 deploy/incus/nrp_cert_sync/test_sync.sh,
which v1 ran against kind in CI. Covers what test_cert_sync.py fakes: the
kubeconfig and its TLS, the strategic-merge patch of a real Secret, the 404 that
turns into a create, and the annotation round trip. The processor
Deployments aren't stood up here, so the roll only skips them (a missing one
is never created); what it patches is pinned in test_cert_sync.py.

Marked ``k8s`` and skipped unless ``FISHSENSE_K8S_ITEST_KUBECONFIG`` names a
disposable cluster (see test_nrp_k8s_integration.py for how to start one).
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from pathlib import Path

import pytest

from fishsense_services_orchestrator.nrp.scaling import kubernetes_apis
from fishsense_services_orchestrator.ops import cert_sync as sut

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
def secret_name(kubeconfig, namespace):
    name = f"itest-temporal-certs-{uuid.uuid4().hex[:8]}"
    yield name
    try:
        kubernetes_apis(kubeconfig).core.delete_namespaced_secret(
            name=name, namespace=namespace
        )
    except Exception:  # pylint: disable=broad-except
        pass


@pytest.fixture
def certs(tmp_path):
    d = tmp_path / "temporal"
    d.mkdir()
    (d / "tls.crt").write_text("leaf-v1\n")
    (d / "tls.key").write_text("key-v1\n")
    (d / "ca.crt").write_text("ca-v1\n")
    return d


def _settings(kubeconfig, namespace, secret_name, certs):
    return sut.CertSyncSettings(
        kubeconfig_path=kubeconfig,
        namespace=namespace,
        secret_name=secret_name,
        client_cert=certs / "tls.crt",
        client_private_key=certs / "tls.key",
        server_root_ca_cert=certs / "ca.crt",
        manifest_dir=Path(__file__).resolve().parents[3] / "deploy" / "nrp",
    )


def _secret(kubeconfig, namespace, name):
    secret = kubernetes_apis(kubeconfig).core.read_namespaced_secret(
        name=name, namespace=namespace
    )
    data = {k: base64.b64decode(v).decode() for k, v in secret.data.items()}
    return data, (secret.metadata.annotations or {})


def test_pushes_skips_rotates_and_recreates(kubeconfig, namespace, secret_name, certs):
    settings = _settings(kubeconfig, namespace, secret_name, certs)

    # 3. the first run creates the Secret, annotated
    assert sut.sync(settings) == sut.Outcome.PUSHED
    data, annotations = _secret(kubeconfig, namespace, secret_name)
    assert data == {
        "client.pem": "leaf-v1\n",
        "client.key": "key-v1\n",
        "root-ca.pem": "ca-v1\n",
    }
    assert annotations[sut.FINGERPRINT_ANNOTATION] == (
        hashlib.sha256(b"leaf-v1\n").hexdigest()
    )

    # 4. an unchanged leaf writes nothing
    before = kubernetes_apis(kubeconfig).core.read_namespaced_secret(
        name=secret_name, namespace=namespace
    )
    assert sut.sync(settings) == sut.Outcome.UNCHANGED
    after = kubernetes_apis(kubeconfig).core.read_namespaced_secret(
        name=secret_name, namespace=namespace
    )
    assert after.metadata.resource_version == before.metadata.resource_version

    # 5. a rotated leaf is pushed
    (certs / "tls.crt").write_text("leaf-v2\n")
    (certs / "tls.key").write_text("key-v2\n")
    assert sut.sync(settings) == sut.Outcome.PUSHED
    data, annotations = _secret(kubeconfig, namespace, secret_name)
    assert (data["client.pem"], data["client.key"]) == ("leaf-v2\n", "key-v2\n")
    assert annotations[sut.FINGERPRINT_ANNOTATION] == (
        hashlib.sha256(b"leaf-v2\n").hexdigest()
    )

    # 6. a deleted Secret is recreated
    kubernetes_apis(kubeconfig).core.delete_namespaced_secret(
        name=secret_name, namespace=namespace
    )
    assert sut.sync(settings) == sut.Outcome.PUSHED
    data, _ = _secret(kubeconfig, namespace, secret_name)
    assert data["client.pem"] == "leaf-v2\n"
