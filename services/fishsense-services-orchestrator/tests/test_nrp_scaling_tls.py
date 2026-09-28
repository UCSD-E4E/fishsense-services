"""TLS verification for the NRP Kubernetes client.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_k8s_scaling_tls.py; v2 builds the core client (for the GPU-fallback
ConfigMap) beside the apps one, from the same relaxed-but-verifying setup.

Python 3.13 turned on ``ssl.VERIFY_X509_STRICT`` by default. NRP/Nautilus's
kubeadm-generated kube-apiserver cert omits the Authority Key Identifier
extension, so strict RFC-5280 verification rejects it ("Missing Authority
Key Identifier") and every call fails the TLS handshake.

`kubernetes_apis` swaps in a context that clears ONLY the strict flag while
keeping full verification. These tests pin the security-critical invariant:
we relax strictness, we do NOT disable verification.
"""

from __future__ import annotations

# Tests exercise an internal helper and pass stub callbacks whose signatures
# must match `load_kube_config` (so some args are intentionally unused).
# pylint: disable=protected-access,unused-argument

import ssl

from kubernetes import config as k8s_config

from fishsense_services_orchestrator.nrp import scaling


def test_relaxed_verification_clears_strict_but_keeps_verification():
    ctx = ssl.create_default_context()
    scaling._apply_relaxed_verification(ctx)

    # the whole point: strict off...
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT
    # ...but still verifying the peer cert + hostname (NOT insecure).
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_kubernetes_apis_inject_a_nonstrict_verifying_context(monkeypatch):
    """The context reaches the urllib3 pool, and ca_certs/cert_reqs are
    dropped so the injected context owns verification (no urllib3 ambiguity).
    Both clients share the one ApiClient, so both get it."""

    def fake_load(config_file, client_configuration):
        client_configuration.verify_ssl = True
        client_configuration.ssl_ca_cert = "/ignored/by/the/stub"
        client_configuration.cert_file = None
        client_configuration.key_file = None
        client_configuration.host = "https://10.0.0.1:443"

    monkeypatch.setattr(k8s_config, "load_kube_config", fake_load)

    sentinel_ctx = ssl.create_default_context()
    monkeypatch.setattr(
        scaling.ssl, "create_default_context", lambda cafile=None: sentinel_ctx
    )

    apis = scaling.kubernetes_apis("/ignored")
    assert apis.core.api_client is apis.apps.api_client
    pool_kw = apis.apps.api_client.rest_client.pool_manager.connection_pool_kw

    assert pool_kw["ssl_context"] is sentinel_ctx
    assert "ca_certs" not in pool_kw
    assert "cert_reqs" not in pool_kw
    assert not sentinel_ctx.verify_flags & ssl.VERIFY_X509_STRICT
    assert sentinel_ctx.verify_mode == ssl.CERT_REQUIRED
    assert sentinel_ctx.check_hostname is True


def test_kubernetes_apis_leave_insecure_kubeconfigs_alone(monkeypatch):
    """If verification is already off (verify_ssl False), don't build a
    context — nothing to relax, and we must not silently re-enable it."""

    def fake_load(config_file, client_configuration):
        client_configuration.verify_ssl = False
        client_configuration.ssl_ca_cert = None
        client_configuration.cert_file = None
        client_configuration.key_file = None
        client_configuration.host = "https://10.0.0.1:443"

    monkeypatch.setattr(k8s_config, "load_kube_config", fake_load)

    def _boom(cafile=None):
        raise AssertionError("must not build a context when verify_ssl is False")

    monkeypatch.setattr(scaling.ssl, "create_default_context", _boom)

    apis = scaling.kubernetes_apis("/ignored")
    pool_kw = apis.apps.api_client.rest_client.pool_manager.connection_pool_kw
    assert "ssl_context" not in pool_kw
