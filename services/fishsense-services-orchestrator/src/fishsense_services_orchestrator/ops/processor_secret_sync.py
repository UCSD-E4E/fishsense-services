"""Keep the NRP processor's Secret, and the weights it names, from the slot.

    python -m fishsense_services_orchestrator.ops.processor_secret_sync

New in v2 (2026-10-15). `fishsense-processor-secrets` is what the processor
reads from its environment on NRP: Garage's endpoint and keys for the labels
bucket (``FISHSENSE_OBJECT_STORE_*``) and for ``model-weights``
(``FISHSENSE_MODEL_WEIGHTS_*``), and the pins of the weights it loads that
fishsense-core does not pin itself
(``FISHSENSE_{SLATE_DETECTOR,BIOCLIP,BIOCLIP_FALLBACK}_{SHA256,SIZE}``).
It was written by hand (deploy/nrp/processor-secret.sh, now gone), so a new weight --
BioCLIP, for the automatic chain's species -- or a rotated key waited on
someone with OpenBao. The slot already has both keys (vault-agent renders
them for this one-shot), so the slot keeps the Secret, on every converge, as
`ops.cert_sync` keeps the Temporal leaf:

1. **every pinned weight is in Garage, as pinned, before the Secret names
   it.** One with a public source (BioCLIP, at its Hugging Face revision) is
   fetched when missing, checked against its sha256 and size, and uploaded;
   one without (run3, the slate detector: uploaded by hand) must be there
   already. A weight of the wrong size, a download that isn't the pinned
   file, a weight that can't be had: nothing is written;
2. **SAM 3.1 is not pinned here.** Until 2026-10-23 the sync kept
   ``FISHSENSE_SAM3_{SHA256,SIZE}`` -- the recorded pin, or one measured from
   Garage's copy when there was none -- because fishsense-core 4.1.0 did not
   pin it. Core does since 4.2.0, the owner matched the recorded pin against
   core's, and the processor now fetches it through core's manifest. The keys
   an earlier sync wrote are not deleted (the write is a patch of the keys
   kept); they are inert, and removing them is an operator's choice;
3. the keys and the pins are written in one patch, with a fingerprint, so a
   converge that changed nothing writes nothing, and a rotated key reaches
   NRP on the next converge. Running pods keep their environment; processors
   are stood up per wake, so the next wake has it;
4. **no copy of the data in an annotation.** The write is a patch (or a
   create), never a client-side apply, and the `last-applied-configuration`
   an earlier `kubectl apply` left -- the Secret's data in full, readable by
   anyone who can `get` it (a dump of that annotation on another Secret
   exposed part of a private key, 2026-10-14) -- is removed.

No kubeconfig (absent, or blank: an unseeded soft render) is nothing to do.
The orchestrator's NRP Role grants `get` and `patch` on this Secret by name
(deploy/nrp/deployer-rbac.yaml).
"""

from __future__ import annotations

import base64
import enum
import hashlib
import logging
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_orchestrator.nrp.scaling import Kubernetes, kubernetes_apis

__all__ = [
    "ENDPOINT",
    "HubFile",
    "MissingWeights",
    "Outcome",
    "PINNED",
    "Pinned",
    "ProcessorSecretSettings",
    "SECRET_NAME",
    "WeightsMismatch",
    "main",
    "sync",
]

log = logging.getLogger("nrp-processor-secret-sync")

SECRET_NAME: Final = "fishsense-processor-secrets"
ENDPOINT: Final = "https://s3.e4e.ucsd.edu"
WEIGHTS_BUCKET: Final = "model-weights"
FINGERPRINT_ANNOTATION: Final = "fishsense.e4e.ucsd.edu/processor-secret-sha256"
LAST_APPLIED: Final = "kubectl.kubernetes.io/last-applied-configuration"
#: The processor's object-store settings that aren't credentials.
OBJECT_STORE: Final = {
    "FISHSENSE_OBJECT_STORE_ENDPOINT_URL": ENDPOINT,
    "FISHSENSE_OBJECT_STORE_REGION": "garage",
    "FISHSENSE_OBJECT_STORE_BUCKET": "labels-fishsense-lite",
    "FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX": "fishsense-lite",
}


@dataclass(frozen=True)
class HubFile:
    """A file of a Hugging Face repo at a revision."""

    repo: str
    revision: str
    filename: str

    @property
    def url(self) -> str:
        return (
            f"https://huggingface.co/{self.repo}/resolve/{self.revision}/"
            f"{self.filename}"
        )


@dataclass(frozen=True)
class Pinned:
    """One weight in ``model-weights``: where, its sha256 and size, the
    processor setting that pins it (``<env>_SHA256``/``<env>_SIZE``; none for
    one its library pins itself), and where to fetch it from when absent."""

    key: str
    sha256: str
    size: int
    env: Optional[str] = None
    source: Optional[HubFile] = None


#: The weights the sync makes sure of. SAM 3.1 is not among them: its
#: checkpoint was uploaded by hand (gated upstream, no public source), and
#: core's manifest pins it.
PINNED: tuple[Pinned, ...] = (
    # fishsense-core 4.1.0's laser detector; core pins it in its manifest.
    Pinned(
        "laser-detector/run3/run3_epoch_021.pt",
        "bd3ab8f5e273",  # a prefix: core's manifest holds the whole
        294473278,
    ),
    # 2026-10-03_slate_detector runs/final-q1, uploaded by hand.
    Pinned(
        "slate-detector/q1/slate_efficientnet_b0.pt",
        "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78",
        16339455,
        env="FISHSENSE_SLATE_DETECTOR",
    ),
    # BioCLIP, at the revisions species_predict/weights.py's architectures
    # were taken from; sha256 and size are Hugging Face's for those files.
    Pinned(
        "bioclip/2.5-vith14/open_clip_model.safetensors",
        "ac2e37c2f89ef8e6b889176a9a3f418970ad9db15a218bd29e3321e95c46ae97",
        3944517804,
        env="FISHSENSE_BIOCLIP",
        source=HubFile(
            "imageomics/bioclip-2.5-vith14",
            "191d741545e4c741cdef4b22c6eb69c945c1e592",
            "open_clip_model.safetensors",
        ),
    ),
    # fishsense-core 4.2.0's SimCC head/tail keypoint model (core pins it in
    # its manifest; owner 2026-10-23: uploaded for the read-only replay).
    Pinned(
        "headtail-keypoints/simcc-sam-e12/simcc_sam_best_length.pt",
        "8918034a0f84f45eb2b8224a369781468ae63497ff06b009bed958ebb82b0110",
        142032463,
        source=HubFile(
            "ucsde4e/fishsense-headtail-keypoint",
            "4a45ac41734662351a334f4992c6fede7ff859f0",
            "simcc_sam_best_length.pt",
        ),
    ),
    Pinned(
        "bioclip/2/open_clip_model.safetensors",
        "b7b2bf6fbc95799e42630e394cf95803892ab447c1a8ab629dbc82fbeaf7dfef",
        1710517724,
        env="FISHSENSE_BIOCLIP_FALLBACK",
        source=HubFile(
            "imageomics/bioclip-2",
            "2957b322090f9cb17ae72c71981c7218a28d81e0",
            "open_clip_model.safetensors",
        ),
    ),
)


class _Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_", extra="ignore")

    nrp_kubeconfig_path: Optional[str] = None
    nrp_namespace: Optional[str] = None
    object_store_access_key_id: Optional[str] = None
    object_store_secret_access_key: Optional[str] = None
    model_weights_access_key_id: Optional[str] = None
    model_weights_secret_access_key: Optional[str] = None


class ProcessorSecretSettings(BaseModel):
    """The cluster, and the two Garage keys the processor is given."""

    kubeconfig_path: Optional[str] = None
    namespace: Optional[str] = None
    object_store_access_key_id: Optional[str] = None
    object_store_secret_access_key: Optional[str] = None
    model_weights_access_key_id: Optional[str] = None
    model_weights_secret_access_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "ProcessorSecretSettings":
        env = _Env()
        return cls(
            kubeconfig_path=env.nrp_kubeconfig_path,
            namespace=env.nrp_namespace,
            object_store_access_key_id=env.object_store_access_key_id,
            object_store_secret_access_key=env.object_store_secret_access_key,
            model_weights_access_key_id=env.model_weights_access_key_id,
            model_weights_secret_access_key=env.model_weights_secret_access_key,
        )


class Outcome(enum.Enum):
    NO_CLUSTER = "no kubeconfig: nothing to sync"
    UNCHANGED = "unchanged: no update"
    WRITTEN = "wrote the Secret"


class MissingWeights(RuntimeError):
    """A pinned weight is not in Garage and has no public source."""


class WeightsMismatch(RuntimeError):
    """A weight is not the pinned file."""


class MissingKeys(RuntimeError):
    """A Garage key to give the processor isn't rendered."""


class _Garage:
    """``model-weights`` over S3, as the model-weights identity."""

    def __init__(self, settings: ProcessorSecretSettings) -> None:
        # pylint: disable-next=import-outside-toplevel
        import boto3
        from botocore.config import Config  # pylint: disable=import-outside-toplevel

        self._s3 = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            region_name="garage",
            aws_access_key_id=settings.model_weights_access_key_id,
            aws_secret_access_key=settings.model_weights_secret_access_key,
            config=Config(s3={"addressing_style": "path"}),
        )

    def size(self, key: str) -> Optional[int]:
        try:
            head = self._s3.head_object(Bucket=WEIGHTS_BUCKET, Key=key)
        except Exception as exc:  # pylint: disable=broad-except
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return int(head["ContentLength"])

    def upload(self, path, key: str) -> None:
        self._s3.upload_file(str(path), WEIGHTS_BUCKET, key)


def _fetch(url: str, path) -> None:
    """Stream ``url`` to ``path``."""
    with urllib.request.urlopen(url, timeout=600) as response, open(path, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure(pin: Pinned, s3: Any, fetch: Callable[[str, Any], None]) -> None:
    """``pin`` is in Garage at its size, uploaded from its source if it was
    missing (checked against its sha256 first)."""
    size = s3.size(pin.key)
    if size == pin.size:
        return
    if size is not None:
        raise WeightsMismatch(
            f"{WEIGHTS_BUCKET}/{pin.key} is {size} bytes, pinned {pin.size}"
        )
    if pin.source is None:
        raise MissingWeights(
            f"{WEIGHTS_BUCKET}/{pin.key} is missing and has no public source"
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(pin.key).name
        log.info("fetching %s for %s", pin.source.url, pin.key)
        fetch(pin.source.url, path)
        got_size, got_sha = path.stat().st_size, _sha256(path)
        if (got_size, got_sha) != (pin.size, pin.sha256):
            raise WeightsMismatch(
                f"{pin.source.url} is {got_size} bytes, sha256 {got_sha}; "
                f"pinned {pin.size}, {pin.sha256}"
            )
        s3.upload(path, pin.key)
        log.info("uploaded %s/%s", WEIGHTS_BUCKET, pin.key)


def _read(core: Any, namespace: str) -> Any:
    try:
        return core.read_namespaced_secret(name=SECRET_NAME, namespace=namespace)
    except Exception as exc:  # pylint: disable=broad-except
        if getattr(exc, "status", None) == 404:
            return None
        raise


def _has_kubeconfig(path: Optional[str]) -> bool:
    if not path or not Path(path).is_file():
        return False
    return bool(Path(path).read_text().strip())


def _keys(settings: ProcessorSecretSettings) -> dict[str, str]:
    keys = {
        "FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID": settings.object_store_access_key_id,
        "FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY": settings.object_store_secret_access_key,
        "FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID": settings.model_weights_access_key_id,
        "FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY": settings.model_weights_secret_access_key,
    }  # fmt: skip
    if missing := sorted(name for name, value in keys.items() if not value):
        raise MissingKeys(f"not rendered: {', '.join(missing)}")
    return keys  # type: ignore[return-value]


def sync(
    settings: ProcessorSecretSettings,
    *,
    kubernetes: Callable[[str], Kubernetes] = kubernetes_apis,
    s3: Callable[[ProcessorSecretSettings], Any] = _Garage,
    fetch: Callable[[str, Any], None] = _fetch,
) -> Outcome:
    """Every pinned weight in Garage, then the Secret, if anything changed."""
    if not _has_kubeconfig(settings.kubeconfig_path):
        log.info("no kubeconfig at %s - nothing to sync", settings.kubeconfig_path)
        return Outcome.NO_CLUSTER
    if not settings.namespace:
        raise ValueError("FISHSENSE_NRP_NAMESPACE is required with a kubeconfig")
    keys = _keys(settings)
    garage = s3(settings)
    for pin in PINNED:
        _ensure(pin, garage, fetch)

    core = kubernetes(settings.kubeconfig_path).core
    current = _read(core, settings.namespace)
    env = {
        **OBJECT_STORE,
        "FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL": ENDPOINT,
        **keys,
    }
    for pin in PINNED:
        if pin.env:
            env[f"{pin.env}_SHA256"] = pin.sha256
            env[f"{pin.env}_SIZE"] = str(pin.size)
    fingerprint = hashlib.sha256(
        "\n".join(f"{k}={env[k]}" for k in sorted(env)).encode()
    ).hexdigest()

    annotations = getattr(getattr(current, "metadata", None), "annotations", None) or {}
    if (
        current is not None
        and annotations.get(FINGERPRINT_ANNOTATION) == fingerprint
        and LAST_APPLIED not in annotations
    ):
        log.info("unchanged (%s) - no update", fingerprint[:12])
        return Outcome.UNCHANGED

    data = {k: base64.b64encode(v.encode()).decode("ascii") for k, v in env.items()}
    metadata = {
        "annotations": {FINGERPRINT_ANNOTATION: fingerprint, LAST_APPLIED: None}
    }
    if current is None:
        core.create_namespaced_secret(
            namespace=settings.namespace,
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": SECRET_NAME,
                    "labels": {"app.kubernetes.io/part-of": "fishsense"},
                    "annotations": {FINGERPRINT_ANNOTATION: fingerprint},
                },
                "data": data,
            },
        )
    else:
        core.patch_namespaced_secret(
            name=SECRET_NAME,
            namespace=settings.namespace,
            body={"metadata": metadata, "data": data},
        )
    log.info("wrote %s/%s (%s)", settings.namespace, SECRET_NAME, fingerprint[:12])
    return Outcome.WRITTEN


def main(settings: ProcessorSecretSettings | None = None) -> int:
    """The one-shot's exit code: 0 when synced or nothing to do, 1 when a key
    isn't rendered or a weight isn't what it is pinned to be."""
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        sync(settings or ProcessorSecretSettings.from_env())
    except (MissingKeys, MissingWeights, WeightsMismatch) as exc:
        log.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
