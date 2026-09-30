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

* **the rollout is keyed on the leaf, not on the push.** v1 rolled every
  Deployment mounting the Secret after a push, as v2 does: the processor reads
  its leaf once, at connect, and a Deployment busy across a rotation is Ready
  with a busy queue, so the sweeper never tears it down and it would hold the
  old leaf past expiry (the 2026-08-14 outage). v2 records the leaf a
  Deployment's pods mount on its pod template -- the sync when it rolls one,
  a wake when it stands one up -- and rolls each existing Deployment whose
  record isn't the current leaf. So a Deployment stood up after the rotation
  isn't restarted for nothing, one the processor role doesn't have up is
  skipped (v1 skipped a missing one too), and a roll that failed is retried by
  the next run even though the Secret is then unchanged (v1's annotate-first
  order made it a silent no-op);
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
from pathlib import Path

import pytest
import yaml

from fishsense_services_orchestrator.nrp.scaling import Kubernetes
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


#: Every processor Deployment (deploy/nrp), each mounting the Secret.
PROCESSORS = [
    "fishsense-processor",
    "fishsense-processor-gpu",
    "fishsense-processor-gpu-cpu-fallback",
    "fishsense-processor-light",
]


class FakeApps:
    """AppsV1Api's Deployment read and strategic-merge patch, over the pod
    template annotations of the Deployments that are up."""

    def __init__(self, up: dict[str, dict] | None = None, *, refuse=()):
        #: name -> its pod template's annotations
        self.up = {name: dict(a) for name, a in (up or {}).items()}
        self.refuse = set(refuse)
        self.rolls: list[str] = []
        self.patches: list[tuple[str, dict, str | None]] = []

    def read_namespaced_deployment(self, name, namespace):
        assert namespace == NAMESPACE
        if name not in self.up:
            raise NotFound()
        template = type("Meta", (), {"annotations": self.up[name] or None})()
        spec = type("Spec", (), {"template": type("T", (), {"metadata": template})()})
        return type("Deployment", (), {"spec": spec()})()

    def patch_namespaced_deployment(self, name, namespace, body, field_manager=None):
        assert namespace == NAMESPACE
        if name in self.refuse:
            raise Forbidden()
        if name not in self.up:
            raise NotFound()
        self.patches.append((name, body, field_manager))
        self.rolls.append(name)
        self.up[name].update(body["spec"]["template"]["metadata"]["annotations"])


class Forbidden(Exception):
    status = 403


def _every_processor_up(annotations: dict | None = None) -> dict[str, dict]:
    return {name: dict(annotations or {}) for name in PROCESSORS}


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
        manifest_dir=MANIFEST_DIR,
    )


def _cluster(core, apps=None):
    return lambda _path: Kubernetes(apps=apps or FakeApps(), core=core)


def _sync(settings, core, apps=None):
    return sut.sync(settings, kubernetes=_cluster(core, apps))


# -- v1's test_sync.sh, case by case ----------------------------------------------


def test_1_an_absent_kubeconfig_is_a_clean_no_op(certs, tmp_path, caplog):
    """The NRP kubeconfig is a soft render: it can legitimately be absent. That
    is not a failure of the stack."""
    caplog.set_level("INFO")
    settings = _settings(certs, tmp_path / "nope")

    assert sut.main(settings, kubernetes=lambda _p: pytest.fail("no cluster")) == 0
    assert "nothing to sync" in caplog.text


def test_1b_no_kubeconfig_configured_is_a_clean_no_op_too(certs):
    """Locally and in e2e NRP is off (no FISHSENSE_NRP_KUBECONFIG_PATH)."""
    settings = _settings(certs, "unused").model_copy(update={"kubeconfig_path": None})

    assert _sync(settings, FakeCore()) == sut.Outcome.NO_CLUSTER


def test_1c_an_empty_kubeconfig_is_the_unseeded_soft_render_a_clean_no_op(
    certs, tmp_path, caplog
):
    """What the slot actually holds before `nrp_orchestrator.kubeconfig` is
    seeded. vault-agent renders a soft template whose secret is missing as an
    EMPTY file, not no file (`{{ with secret }}` emits nothing), so "absent"
    must include it. Otherwise this one-shot fails on every converge and every
    rotation until NRP is seeded -- and deploy/incus/cert-sync-timer.nix
    re-runs it every six hours, failing each time."""
    caplog.set_level("INFO")
    empty = tmp_path / "kubeconfig"
    empty.write_text("\n")

    assert (
        sut.main(_settings(certs, empty), kubernetes=lambda _p: pytest.fail("no"))
        == 0
    )
    assert "nothing to sync" in caplog.text


def test_2_a_missing_cert_render_is_a_hard_error(certs, kubeconfig):
    (certs / "tls.crt").unlink()

    assert sut.main(_settings(certs, kubeconfig), kubernetes=_cluster(FakeCore())) == 1


def test_3_the_first_run_pushes_the_leaf_and_rolls_every_deployment(certs, kubeconfig):
    core, apps = FakeCore(), FakeApps(_every_processor_up())

    assert _sync(_settings(certs, kubeconfig), core, apps) == sut.Outcome.PUSHED

    assert _decoded(core) == {
        "client.pem": "leaf-v1\n",
        "client.key": "key-v1\n",
        "root-ca.pem": "ca-v1\n",
    }
    expected = hashlib.sha256(b"leaf-v1\n").hexdigest()
    assert core.secret["metadata"]["annotations"][FP] == expected
    # v1's "EVERY Deployment that mounts the Secret must be rolled".
    assert sorted(apps.rolls) == PROCESSORS
    assert all(apps.up[name] == {FP: expected} for name in PROCESSORS)


def test_4_an_unchanged_leaf_is_not_pushed_again_nor_rolled(certs, kubeconfig):
    core, apps = FakeCore(), FakeApps(_every_processor_up())
    _sync(_settings(certs, kubeconfig), core, apps)
    core.writes.clear()
    apps.rolls.clear()

    assert _sync(_settings(certs, kubeconfig), core, apps) == sut.Outcome.UNCHANGED
    assert core.writes == []
    assert apps.rolls == []


def test_5_a_rotated_leaf_is_pushed_and_rolls_every_deployment(certs, kubeconfig):
    core, apps = FakeCore(), FakeApps(_every_processor_up())
    _sync(_settings(certs, kubeconfig), core, apps)
    apps.rolls.clear()
    (certs / "tls.crt").write_text("leaf-v2\n")
    (certs / "tls.key").write_text("key-v2\n")

    assert _sync(_settings(certs, kubeconfig), core, apps) == sut.Outcome.PUSHED

    assert _decoded(core)["client.pem"] == "leaf-v2\n"
    assert _decoded(core)["client.key"] == "key-v2\n"
    rotated = hashlib.sha256(b"leaf-v2\n").hexdigest()
    assert core.secret["metadata"]["annotations"][FP] == rotated
    assert core.writes == ["create", "patch"]
    assert sorted(apps.rolls) == PROCESSORS
    assert all(apps.up[name] == {FP: rotated} for name in PROCESSORS)


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


def test_a_deployment_that_is_down_is_skipped_not_created(certs, kubeconfig):
    """v2 stands a role up only while it has work (PLAN.md §3), so most of the
    time most of them don't exist; the next wake's pods mount the new leaf.
    Rolling means patching, which must never bring one back (v1 skipped a
    missing Deployment too)."""
    apps = FakeApps({"fishsense-processor-light": {}})

    assert _sync(_settings(certs, kubeconfig), FakeCore(), apps) == sut.Outcome.PUSHED

    assert apps.rolls == ["fishsense-processor-light"]
    assert set(apps.up) == {"fishsense-processor-light"}


def test_a_deployment_stood_up_on_the_current_leaf_is_not_restarted(certs, kubeconfig):
    """A wake stamps the leaf its pods mount; a Deployment stood up after the
    push already runs on it, and restarting it would only interrupt its work."""
    current = hashlib.sha256(b"leaf-v1\n").hexdigest()
    apps = FakeApps(
        {"fishsense-processor": {FP: "an-older-leaf"}, "fishsense-processor-gpu": {FP: current}}  # fmt: skip
    )

    _sync(_settings(certs, kubeconfig), FakeCore(), apps)

    assert apps.rolls == ["fishsense-processor"]


def test_the_roll_touches_only_the_pod_templates_leaf(certs, kubeconfig):
    """A strategic-merge patch of one pod-template annotation -- which is what
    `kubectl rollout restart` does with its own -- under a field manager of
    its own, so the orchestrator's next server-side apply of the manifest
    (`nrp.scaling.FIELD_MANAGER`) doesn't strip it and roll the pods again."""
    apps = FakeApps({"fishsense-processor": {}})

    _sync(_settings(certs, kubeconfig), FakeCore(), apps)

    ((name, body, manager),) = apps.patches
    assert name == "fishsense-processor"
    fingerprint = hashlib.sha256(b"leaf-v1\n").hexdigest()
    assert body == {"spec": {"template": {"metadata": {"annotations": {FP: fingerprint}}}}}  # fmt: skip
    assert manager == sut.FIELD_MANAGER
    assert manager != "fishsense-orchestrator"


def test_a_roll_that_failed_is_retried_by_the_next_run(certs, kubeconfig):
    """v1 annotated the Secret before rolling, so a roll that failed left a
    Secret that read as current and every later run a no-op: the unrolled
    Deployment kept the old leaf until it expired. v2 decides each roll from
    the Deployment's own record, so the next converge finishes the job."""
    core = FakeCore()
    apps = FakeApps(_every_processor_up(), refuse={"fishsense-processor-gpu"})
    with pytest.raises(Forbidden):
        _sync(_settings(certs, kubeconfig), core, apps)
    apps.refuse.clear()
    apps.rolls.clear()

    assert _sync(_settings(certs, kubeconfig), core, apps) == sut.Outcome.UNCHANGED

    fingerprint = hashlib.sha256(b"leaf-v1\n").hexdigest()
    assert "fishsense-processor-gpu" in apps.rolls
    assert all(apps.up[name] == {FP: fingerprint} for name in PROCESSORS)


def test_it_rolls_exactly_the_deployments_that_mount_the_secret():
    """The four processor Deployments, by their manifests' names -- one place
    a name is written, so a new role can't be missed the way v1's first
    forwarder missed the GPU pair."""
    settings = sut.CertSyncSettings(manifest_dir=MANIFEST_DIR)

    assert sorted(sut.processor_deployments(settings)) == PROCESSORS


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


def test_the_orchestrators_role_may_roll_the_deployments():
    """The roll reads each Deployment and patches its pod template: `get` and
    `patch` on apps/deployments, which the wakes already hold."""
    docs = list(yaml.safe_load_all((MANIFEST_DIR / "deployer-rbac.yaml").read_text()))
    (role,) = [d for d in docs if d and d["kind"] == "Role"]
    (deployments,) = [r for r in role["rules"] if "deployments" in r["resources"]]

    assert deployments["apiGroups"] == ["apps"]
    assert "resourceNames" not in deployments
    assert {"get", "patch"} <= set(deployments["verbs"])
