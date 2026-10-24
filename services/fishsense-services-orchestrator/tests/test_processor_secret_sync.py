"""The processor's NRP Secret, kept by the slot on every converge.

New in v2 (2026-10-15). `fishsense-processor-secrets` holds what the NRP
processor reads from its environment -- Garage's endpoint and keys for the
labels bucket and for `model-weights`, and the pins of the weights it loads --
and was written by hand (deploy/nrp/processor-secret.sh), so a new weight
(BioCLIP, for the automatic chain's species) or a rotated key waited on
someone. Now the slot keeps it, as `ops.cert_sync` keeps the Temporal leaf.

Pinned here:

* **every pinned weight is in Garage before the Secret names it.** One with a
  public source (BioCLIP, at its Hugging Face revision) is fetched, checked
  against its sha256 and size, and uploaded; one without (run3, the slate
  detector) must already be there. Anything wrong writes nothing;
* **SAM 3.1's pin is the one already recorded**, measured from Garage only
  when there is none (its checkpoint was uploaded by hand; nothing else pins
  it);
* the Secret carries the credentials and the pins in one write, with a
  fingerprint, so a converge that changed nothing writes nothing; a rotated
  key is written on the next converge;
* **no copy of the data sits in an annotation**: the write is a patch, never
  a client-side apply, and the `last-applied-configuration` an earlier
  `kubectl apply` left is removed (a dump of that annotation exposed part of
  a private key on 2026-10-14);
* no kubeconfig (absent, or blank: an unseeded render) is nothing to do.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from fishsense_services_orchestrator.nrp.scaling import Kubernetes
from fishsense_services_orchestrator.ops import processor_secret_sync as sut

NAMESPACE = "e4e-fishsense"
SECRET = "fishsense-processor-secrets"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


class NotFound(Exception):
    status = 404


class _Obj:
    def __init__(self, secret):
        self.metadata = type(
            "M", (), {"annotations": secret["metadata"].get("annotations")}
        )()
        self.data = secret["data"]


class FakeCore:
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
        self.secret["data"].update(body.get("data", {}))
        annotations = self.secret["metadata"].setdefault("annotations", {})
        for key, value in body["metadata"].get("annotations", {}).items():
            if value is None:
                annotations.pop(key, None)
            else:
                annotations[key] = value

    def create_namespaced_secret(self, namespace, body):
        assert namespace == NAMESPACE and body["metadata"]["name"] == SECRET
        assert "kubectl.kubernetes.io/last-applied-configuration" not in (
            body["metadata"].get("annotations") or {}
        )
        self.writes.append("create")
        self.secret = {"metadata": dict(body["metadata"]), "data": dict(body["data"])}


class FakeS3:
    """Garage's model-weights bucket: key -> bytes."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.uploads: list[str] = []
        self.downloads: list[str] = []

    def size(self, key: str) -> int | None:
        body = self.objects.get(key)
        return None if body is None else len(body)

    def upload(self, path, key: str) -> None:
        self.uploads.append(key)
        self.objects[key] = open(path, "rb").read()

    def download(self, key: str, path) -> None:
        self.downloads.append(key)
        open(path, "wb").write(self.objects[key])


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


BIOCLIP = b"bioclip-2.5 weights"
FALLBACK = b"bioclip-2 weights"
RUN3 = b"run3 weights"
SLATE = b"slate weights"
SAM = b"sam 3.1 weights"
HUB = {
    "https://huggingface.co/imageomics/a/resolve/r1/open_clip_model.safetensors": BIOCLIP,
    "https://huggingface.co/imageomics/b/resolve/r2/open_clip_model.safetensors": FALLBACK,
}


@pytest.fixture(autouse=True)
def pins(monkeypatch):
    """Small stand-ins for the real (multi-GB) weights."""
    monkeypatch.setattr(sut, "PINNED", (
        sut.Pinned("laser-detector/run3/run3_epoch_021.pt", _sha(RUN3), len(RUN3)),
        sut.Pinned("slate-detector/q1/slate_efficientnet_b0.pt", _sha(SLATE),
                   len(SLATE), env="FISHSENSE_SLATE_DETECTOR"),
        sut.Pinned("bioclip/2.5-vith14/open_clip_model.safetensors", _sha(BIOCLIP),
                   len(BIOCLIP), env="FISHSENSE_BIOCLIP",
                   source=sut.HubFile("imageomics/a", "r1", "open_clip_model.safetensors")),
        sut.Pinned("bioclip/2/open_clip_model.safetensors", _sha(FALLBACK),
                   len(FALLBACK), env="FISHSENSE_BIOCLIP_FALLBACK",
                   source=sut.HubFile("imageomics/b", "r2", "open_clip_model.safetensors")),
    ))  # fmt: skip


def _garage(**missing) -> FakeS3:
    objects = {
        "laser-detector/run3/run3_epoch_021.pt": RUN3,
        "slate-detector/q1/slate_efficientnet_b0.pt": SLATE,
        "bioclip/2.5-vith14/open_clip_model.safetensors": BIOCLIP,
        "bioclip/2/open_clip_model.safetensors": FALLBACK,
        sut.SAM_KEY: SAM,
    }
    for key in missing.get("keys", ()):
        objects.pop(key)
    return FakeS3(objects)


def _settings(
    tmp_path, *, kubeconfig="apiVersion: v1\n", keys=("ak", "sk", "mk", "ms")
):
    path = tmp_path / "kubeconfig"
    if kubeconfig is not None:
        path.write_text(kubeconfig)
    return sut.ProcessorSecretSettings(
        kubeconfig_path=str(path),
        namespace=NAMESPACE,
        object_store_access_key_id=keys[0],
        object_store_secret_access_key=keys[1],
        model_weights_access_key_id=keys[2],
        model_weights_secret_access_key=keys[3],
    )


def _sync(settings, core, s3, fetched=None):
    def fetch(url, path):
        if fetched is not None:
            fetched.append(url)
        open(path, "wb").write(HUB[url])

    return sut.sync(
        settings,
        kubernetes=lambda _path: Kubernetes(core=core, apps=None),
        s3=lambda _settings: s3,
        fetch=fetch,
    )


def _env(core) -> dict[str, str]:
    return {k: base64.b64decode(v).decode() for k, v in core.secret["data"].items()}


# -- the first converge ---------------------------------------------------------------


def test_the_first_converge_writes_every_key_the_processor_reads(tmp_path):
    core = FakeCore()

    assert _sync(_settings(tmp_path), core, _garage()) is sut.Outcome.WRITTEN

    env = _env(core)
    assert env["FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID"] == "ak"
    assert env["FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY"] == "sk"
    assert env["FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID"] == "mk"
    assert env["FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY"] == "ms"
    assert env["FISHSENSE_OBJECT_STORE_ENDPOINT_URL"] == sut.ENDPOINT
    assert env["FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL"] == sut.ENDPOINT
    assert env["FISHSENSE_BIOCLIP_SHA256"] == _sha(BIOCLIP)
    assert env["FISHSENSE_BIOCLIP_SIZE"] == str(len(BIOCLIP))
    assert env["FISHSENSE_BIOCLIP_FALLBACK_SHA256"] == _sha(FALLBACK)
    assert env["FISHSENSE_SLATE_DETECTOR_SHA256"] == _sha(SLATE)
    # SAM 3.1: no recorded pin, so measured from Garage.
    assert env["FISHSENSE_SAM3_SHA256"] == _sha(SAM)
    assert env["FISHSENSE_SAM3_SIZE"] == str(len(SAM))


def test_a_converge_that_changed_nothing_writes_nothing(tmp_path):
    core = FakeCore()
    _sync(_settings(tmp_path), core, _garage())
    core.writes.clear()

    assert _sync(_settings(tmp_path), core, _garage()) is sut.Outcome.UNCHANGED
    assert core.writes == []


def test_a_rotated_key_is_written_on_the_next_converge(tmp_path):
    core = FakeCore()
    _sync(_settings(tmp_path), core, _garage())

    _sync(_settings(tmp_path, keys=("ak2", "sk2", "mk", "ms")), core, _garage())

    assert _env(core)["FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID"] == "ak2"


def test_the_recorded_sam_pin_is_kept_and_sam_is_not_downloaded(tmp_path):
    core = FakeCore()
    s3 = _garage()
    _sync(_settings(tmp_path), core, s3)
    s3.downloads.clear()

    _sync(_settings(tmp_path, keys=("ak2", "sk", "mk", "ms")), core, s3)

    assert s3.downloads == []
    assert _env(core)["FISHSENSE_SAM3_SHA256"] == _sha(SAM)


# -- the weights ------------------------------------------------------------------------


def test_a_missing_hub_weight_is_fetched_checked_and_uploaded(tmp_path):
    core, fetched = FakeCore(), []
    s3 = _garage(keys=["bioclip/2.5-vith14/open_clip_model.safetensors"])

    _sync(_settings(tmp_path), core, s3, fetched)

    assert fetched == [
        "https://huggingface.co/imageomics/a/resolve/r1/open_clip_model.safetensors"
    ]
    assert s3.uploads == ["bioclip/2.5-vith14/open_clip_model.safetensors"]
    assert s3.objects["bioclip/2.5-vith14/open_clip_model.safetensors"] == BIOCLIP


def test_a_weight_already_there_is_not_fetched(tmp_path):
    fetched = []
    _sync(_settings(tmp_path), FakeCore(), _garage(), fetched)
    assert fetched == []


def test_a_hub_file_that_is_not_the_pinned_one_is_never_uploaded(tmp_path, monkeypatch):
    core = FakeCore()
    s3 = _garage(keys=["bioclip/2/open_clip_model.safetensors"])
    monkeypatch.setitem(HUB, "https://huggingface.co/imageomics/b/resolve/r2/"
                        "open_clip_model.safetensors", b"something else")  # fmt: skip

    with pytest.raises(sut.WeightsMismatch):
        _sync(_settings(tmp_path), core, s3)

    assert s3.uploads == [] and core.secret is None


def test_a_weight_with_no_public_source_must_already_be_in_garage(tmp_path):
    core = FakeCore()
    with pytest.raises(sut.MissingWeights):
        _sync(_settings(tmp_path), core,
              _garage(keys=["slate-detector/q1/slate_efficientnet_b0.pt"]))  # fmt: skip
    assert core.secret is None


def test_a_weight_of_the_wrong_size_in_garage_is_refused(tmp_path):
    s3 = _garage()
    s3.objects["laser-detector/run3/run3_epoch_021.pt"] = b"truncated"
    with pytest.raises(sut.WeightsMismatch):
        _sync(_settings(tmp_path), FakeCore(), s3)


# -- no copy in an annotation, and nothing to do --------------------------------------


def test_the_copy_an_earlier_kubectl_apply_left_is_removed(tmp_path):
    core = FakeCore({
        "metadata": {"annotations": {LAST_APPLIED: '{"data": {"a": "secret"}}'}},
        "data": {},
    })  # fmt: skip

    _sync(_settings(tmp_path), core, _garage())

    assert LAST_APPLIED not in core.secret["metadata"]["annotations"]


@pytest.mark.parametrize("kubeconfig", [None, "", "  \n"])
def test_no_kubeconfig_is_nothing_to_do(tmp_path, kubeconfig):
    core = FakeCore()
    outcome = _sync(_settings(tmp_path, kubeconfig=kubeconfig), core, FakeS3())
    assert outcome is sut.Outcome.NO_CLUSTER and core.writes == []


def test_the_real_pins_name_what_the_processor_fetches():
    """The module's own pins, unpatched: BioCLIP at the paths and revisions
    species_predict/weights.py reads, with Hugging Face's sha256 and size."""
    import importlib

    real = importlib.reload(sut).PINNED
    by_key = {p.key: p for p in real}
    bioclip = by_key["bioclip/2.5-vith14/open_clip_model.safetensors"]
    assert bioclip.sha256.startswith("ac2e37c2") and bioclip.size == 3944517804
    assert bioclip.source.revision == "191d741545e4c741cdef4b22c6eb69c945c1e592"
    fallback = by_key["bioclip/2/open_clip_model.safetensors"]
    assert fallback.sha256.startswith("b7b2bf6f") and fallback.size == 1710517724
    assert by_key["slate-detector/q1/slate_efficientnet_b0.pt"].size == 16339455


def test_the_processor_loads_its_weights_from_what_the_sync_writes(
    tmp_path, monkeypatch
):
    """Cross-package: the pins the sync writes satisfy the processor's own
    settings, and BioCLIP lands where the processor fetches it."""
    import importlib

    from fishsense_services_processor.headtail_predict.weights import Sam3Settings
    from fishsense_services_processor.slate_detect.weights import SlateDetectorSettings
    from fishsense_services_processor.species_predict.weights import (
        BIOCLIP_FILENAME,
        BIOCLIP_MODEL,
        BIOCLIP_VERSIONS,
        BioclipSettings,
    )

    real = importlib.reload(sut).PINNED
    keys = {p.key for p in real}
    for version in BIOCLIP_VERSIONS.values():
        assert f"{BIOCLIP_MODEL}/{version}/{BIOCLIP_FILENAME}" in keys

    core = FakeCore()
    monkeypatch.setattr(sut, "PINNED", real)
    objects = {p.key: b"" for p in real}
    s3 = FakeS3({**objects, sut.SAM_KEY: SAM})
    s3.size = lambda key: next((p.size for p in real if p.key == key), len(SAM))
    _sync(_settings(tmp_path), core, s3)

    for key, value in _env(core).items():
        monkeypatch.setenv(key, value)
    BioclipSettings()
    SlateDetectorSettings()
    Sam3Settings()


def test_the_simcc_keypoint_model_is_pinned_as_core_pins_it():
    """The SimCC head/tail model (fishsense-core, pinned since 4.2.0; owner 2026-10-23):
    at the key the processor's verified fetch reads, with core's sha256 and
    size, fetched from core's pinned Hugging Face revision when missing."""
    import importlib

    from fishsense_core.models import builtin_manifest
    from fishsense_services_processor.weights import model_key

    ref = builtin_manifest().resolve("headtail-keypoints", "simcc-sam-e12")
    pins = {p.key: p for p in importlib.reload(sut).PINNED}
    pin = pins[model_key(ref.name, ref.version, ref.filename)]
    assert (pin.sha256, pin.size) == (ref.sha256, ref.size)
    hub = ref.origins["huggingface"]
    assert (pin.source.repo, pin.source.revision, pin.source.filename) == (
        hub["repo"],
        hub["revision"],
        ref.filename,
    )
