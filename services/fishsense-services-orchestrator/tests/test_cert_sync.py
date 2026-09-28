"""Mirroring the slot's rotated Temporal leaf into the processor's NRP Secret.

Ported from fishsense-lite@77e8f8e5 deploy/incus/nrp_cert_sync/sync.sh and its
test, deploy/incus/nrp_cert_sync/test_sync.sh (cases 1-6 below, in its order).

Why it exists: the krg-prod Temporal leaf is a 7-day cert that vault-agent
re-renders on the slot. The processor runs on NRP, out of vault-agent's reach,
and holds the same identity (CN=fishsense-worker) in a Kubernetes Secret. In v1
nothing renewed that copy: it expired 2026-08-14 and every pod crash-looped on
`CertificateExpired`. The slot is where the renewed leaf lands, so the slot
pushes it on, whenever it rotates (the `temporal.reload` hook) and on every
converge -- a no-op when the leaf is unchanged.

v2 changes, each pinned here:

* **no rollout restart.** v1 rolled every Deployment that mounted the Secret,
  because its Deployments lived forever at zero replicas and a scale-up reused
  the old pod template. v2 stands the processor up per wake and tears it down
  when idle (PLAN.md §3), so the next wake's pods mount the current Secret. A
  pod that outlives a rotation is still covered: kubelet refreshes a mounted
  Secret in place, so a container that restarts on an expired leaf reads the
  new one;
* one write, not two: the Secret's keys and its fingerprint annotation land
  together (v1 applied, then annotated);
* Python on the orchestrator's image, reusing the NRP stage's kubeconfig
  handling (and its TLS relaxation for NRP's apiserver cert), where v1 was a
  shell script in an `alpine/kubectl` container; the leaf is the orchestrator's
  own Temporal client cert (``FISHSENSE_TEMPORAL_*``), and the cluster its NRP
  settings (``FISHSENSE_NRP_*``).
"""

from __future__ import annotations

import base64
import hashlib
import inspect
from pathlib import Path

import pytest
import yaml

from fishsense_services_orchestrator.ops import cert_sync as sut

NAMESPACE = "e4e-fishsense"
SECRET = "fishsense-data-worker-temporal-certs"
FP = "fishsense.e4e.ucsd.edu/leaf-sha256"
MANIFEST_DIR = Path(__file__).resolve().parents[3] / "deploy/nrp"


class NotFound(Exception):
    status = 404


class FakeCore:
    """CoreV1Api's Secret calls, over one in-memory Secret."""

    def __init__(self, secret: dict | None = None):
        self.secret = secret
        self.writes: list[str] = []

    def read_namespaced_secret(self, name, namespace):
        assert (name, namespace) == (SECRET, NAMESPACE)
        if self.secret is None:
            raise NotFound()
        return _Obj(self.secret)

    def patch_namespaced_secret(self, name, namespace, body):
        assert (name, namespace) == (SECRET, NAMESPACE)
        if self.secret is None:
            raise NotFound()
        self.writes.append("patch")
        self.secret["data"].update(body["data"])
        self.secret["metadata"].setdefault("annotations", {}).update(
            body["metadata"]["annotations"]
        )

    def create_namespaced_secret(self, namespace, body):
        assert namespace == NAMESPACE
        assert body["metadata"]["name"] == SECRET
        self.writes.append("create")
        self.secret = {"metadata": dict(body["metadata"]), "data": dict(body["data"])}


class _Obj:
    """What the kubernetes client returns: attributes, not keys."""

    def __init__(self, secret):
        self.metadata = type(
            "Meta", (), {"annotations": secret["metadata"].get("annotations")}
        )()
        self.data = secret["data"]


def _decoded(core: FakeCore) -> dict:
    return {k: base64.b64decode(v).decode() for k, v in core.secret["data"].items()}


@pytest.fixture
def certs(tmp_path):
    """The slot's vault-agent render: plain text is enough, since the sync
    never parses a cert -- it hashes tls.crt and forwards all three."""
    d = tmp_path / "temporal"
    d.mkdir()
    (d / "tls.crt").write_text("leaf-v1\n")
    (d / "tls.key").write_text("key-v1\n")
    (d / "ca.crt").write_text("ca-v1\n")
    return d


@pytest.fixture
def kubeconfig(tmp_path):
    path = tmp_path / "kubeconfig"
    path.write_text("apiVersion: v1\nkind: Config\n")
    return path


def _settings(certs, kubeconfig):
    return sut.CertSyncSettings(
        kubeconfig_path=str(kubeconfig),
        namespace=NAMESPACE,
        client_cert=certs / "tls.crt",
        client_private_key=certs / "tls.key",
        server_root_ca_cert=certs / "ca.crt",
    )


def _sync(settings, core):
    return sut.sync(settings, core_factory=lambda _path: core)


# -- v1's test_sync.sh, case by case ----------------------------------------------


def test_1_an_absent_kubeconfig_is_a_clean_no_op(certs, tmp_path, caplog):
    """The NRP kubeconfig is a soft render: it can legitimately be absent. That
    is not a failure of the stack."""
    caplog.set_level("INFO")
    settings = _settings(certs, tmp_path / "nope")

    assert sut.main(settings, core_factory=lambda _p: pytest.fail("no cluster")) == 0
    assert "nothing to sync" in caplog.text


def test_1b_no_kubeconfig_configured_is_a_clean_no_op_too(certs):
    """Locally and in e2e NRP is off (no FISHSENSE_NRP_KUBECONFIG_PATH)."""
    settings = _settings(certs, "unused").model_copy(update={"kubeconfig_path": None})

    assert _sync(settings, FakeCore()) == sut.Outcome.NO_CLUSTER


def test_2_a_missing_cert_render_is_a_hard_error(certs, kubeconfig):
    (certs / "tls.crt").unlink()

    assert (
        sut.main(_settings(certs, kubeconfig), core_factory=lambda _p: FakeCore()) == 1
    )


def test_3_the_first_run_pushes_the_leaf_and_records_its_fingerprint(certs, kubeconfig):
    core = FakeCore()

    assert _sync(_settings(certs, kubeconfig), core) == sut.Outcome.PUSHED

    assert _decoded(core) == {
        "client.pem": "leaf-v1\n",
        "client.key": "key-v1\n",
        "root-ca.pem": "ca-v1\n",
    }
    expected = hashlib.sha256(b"leaf-v1\n").hexdigest()
    assert core.secret["metadata"]["annotations"][FP] == expected


def test_4_an_unchanged_leaf_is_not_pushed_again(certs, kubeconfig):
    core = FakeCore()
    _sync(_settings(certs, kubeconfig), core)
    core.writes.clear()

    assert _sync(_settings(certs, kubeconfig), core) == sut.Outcome.UNCHANGED
    assert core.writes == []


def test_5_a_rotated_leaf_is_pushed(certs, kubeconfig):
    core = FakeCore()
    _sync(_settings(certs, kubeconfig), core)
    (certs / "tls.crt").write_text("leaf-v2\n")
    (certs / "tls.key").write_text("key-v2\n")

    assert _sync(_settings(certs, kubeconfig), core) == sut.Outcome.PUSHED

    assert _decoded(core)["client.pem"] == "leaf-v2\n"
    assert _decoded(core)["client.key"] == "key-v2\n"
    assert core.secret["metadata"]["annotations"][FP] == (
        hashlib.sha256(b"leaf-v2\n").hexdigest()
    )
    assert core.writes == ["create", "patch"]


def test_6_a_deleted_secret_is_recreated(certs, kubeconfig):
    core = FakeCore()
    _sync(_settings(certs, kubeconfig), core)
    core.secret = None

    assert _sync(_settings(certs, kubeconfig), core) == sut.Outcome.PUSHED
    assert _decoded(core)["client.pem"] == "leaf-v1\n"


# -- v2 -------------------------------------------------------------------------------


def test_it_reads_the_orchestrators_own_nrp_and_temporal_settings(monkeypatch):
    """The same cluster the NRP stage reaches, and the same leaf the
    orchestrator connects to Temporal with."""
    monkeypatch.setenv("FISHSENSE_NRP_KUBECONFIG_PATH", "/run/tenant/nrp/kubeconfig")
    monkeypatch.setenv("FISHSENSE_NRP_NAMESPACE", NAMESPACE)
    monkeypatch.setenv("FISHSENSE_TEMPORAL_CLIENT_CERT", "/run/tenant/temporal/tls.crt")
    monkeypatch.setenv(
        "FISHSENSE_TEMPORAL_CLIENT_PRIVATE_KEY", "/run/tenant/temporal/tls.key"
    )
    monkeypatch.setenv(
        "FISHSENSE_TEMPORAL_SERVER_ROOT_CA_CERT", "/run/tenant/temporal/ca.crt"
    )

    settings = sut.CertSyncSettings.from_env()

    assert settings.kubeconfig_path == "/run/tenant/nrp/kubeconfig"
    assert settings.namespace == NAMESPACE
    assert settings.secret_name == SECRET
    assert settings.client_cert == Path("/run/tenant/temporal/tls.crt")
    assert settings.client_private_key == Path("/run/tenant/temporal/tls.key")
    assert settings.server_root_ca_cert == Path("/run/tenant/temporal/ca.crt")


def test_a_kubeconfig_without_a_namespace_is_refused(certs, kubeconfig):
    settings = _settings(certs, kubeconfig).model_copy(update={"namespace": None})

    with pytest.raises(ValueError, match="NAMESPACE"):
        _sync(settings, FakeCore())


def test_a_secret_without_the_fingerprint_is_pushed(certs, kubeconfig):
    """A hand-minted Secret (how v1's first one was made) has no annotation:
    it can't be known current, so it is replaced."""
    core = FakeCore(
        {"metadata": {"name": SECRET}, "data": {"client.pem": "b2xk"}}  # "old"
    )

    assert _sync(_settings(certs, kubeconfig), core) == sut.Outcome.PUSHED
    assert core.writes == ["patch"]
    assert _decoded(core)["client.pem"] == "leaf-v1\n"


def test_a_cluster_error_propagates(certs, kubeconfig):
    """Only "no such Secret" means create; anything else (a 403 from a Role
    missing the Secret rules) must fail the run, loudly."""

    class Forbidden(Exception):
        status = 403

    class DeniedCore(FakeCore):
        def read_namespaced_secret(self, name, namespace):
            raise Forbidden()

    with pytest.raises(Forbidden):
        _sync(_settings(certs, kubeconfig), DeniedCore())


def test_a_refused_update_is_not_retried_as_a_create(certs, kubeconfig):
    """Only a missing Secret is created; a refused patch fails the run."""

    class Forbidden(Exception):
        status = 403

    class RefusingCore(FakeCore):
        def patch_namespaced_secret(self, name, namespace, body):
            raise Forbidden()

    core = RefusingCore({"metadata": {"name": SECRET}, "data": {}})
    with pytest.raises(Forbidden):
        _sync(_settings(certs, kubeconfig), core)
    assert core.writes == []


def test_the_pushed_keys_are_what_every_processor_deployment_mounts():
    """The Secret's name and its three keys are what deploy/nrp's Deployments
    mount at /certs and point their Temporal settings at. Drift here is the
    2026-08-14 outage again: a pod starting on a leaf nobody renews."""
    deployments = [
        (manifest, body)
        for manifest in sorted(MANIFEST_DIR.glob("*.yaml"))
        for body in yaml.safe_load_all(manifest.read_text())
        if body and body.get("kind") == "Deployment"
    ]
    assert len(deployments) == 4
    for manifest, body in deployments:
        pod = body["spec"]["template"]["spec"]
        (volume,) = [v for v in pod["volumes"] if v["name"] == "temporal-certs"]
        assert volume["secret"]["secretName"] == sut.DEFAULT_SECRET_NAME, manifest
        (container,) = pod["containers"]
        env = {e["name"]: e.get("value") for e in container["env"]}
        (mount,) = [
            m for m in container["volumeMounts"] if m["name"] == "temporal-certs"
        ]
        root = mount["mountPath"]
        assert env["FISHSENSE_TEMPORAL_CLIENT_CERT"] == f"{root}/{sut.CERT_KEY}"
        assert env["FISHSENSE_TEMPORAL_CLIENT_PRIVATE_KEY"] == f"{root}/{sut.KEY_KEY}"
        assert env["FISHSENSE_TEMPORAL_SERVER_ROOT_CA_CERT"] == f"{root}/{sut.CA_KEY}"


def test_it_never_touches_a_deployment():
    """v2: no rollout restart (see the module docstring)."""
    source = inspect.getsource(sut)
    for forbidden in ("AppsV1", ".apps", "namespaced_deployment", "restartedAt"):
        assert forbidden not in source, forbidden


def test_the_orchestrators_role_may_write_only_that_secret():
    """The sync runs as the orchestrator's NRP identity: create (RBAC can't
    scope a create by name) and get/patch pinned to this one Secret -- never
    `list`, which would hand over every Secret in the namespace (v1's split)."""
    docs = list(yaml.safe_load_all((MANIFEST_DIR / "deployer-rbac.yaml").read_text()))
    (role,) = [d for d in docs if d and d["kind"] == "Role"]
    secret_rules = [r for r in role["rules"] if "secrets" in r["resources"]]

    unnamed = [r for r in secret_rules if "resourceNames" not in r]
    named = [r for r in secret_rules if "resourceNames" in r]
    assert [sorted(r["verbs"]) for r in unnamed] == [["create"]]
    assert [(r["resourceNames"], sorted(r["verbs"])) for r in named] == [
        ([sut.DEFAULT_SECRET_NAME], ["get", "patch"])
    ]
