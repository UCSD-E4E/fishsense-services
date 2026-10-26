"""Model weights from Garage's `model-weights` bucket, verified by fishsense-core.

Ported from fishsense-lite@77e8f8e5: `model_key` (libs/fishsense-shared/src/
fishsense_shared/object_store.py), the data-worker's `download_model`
(object_store.py) and its `ensure_checkpoint` (checkpoint_cache.py). v1 kept
weights in the object store rather than in the image, and v2 keeps that for the
same reasons: the processor is stood up per wake (PLAN.md §3), so a
multi-gigabyte layer would be pulled on every cold start, and weights whose
upstream distribution is gated should not travel in a pullable artifact.

PLAN.md §9.12 decided the shape: the bucket, as v1 lays it out
(`{name}/{version}/{filename}`), behind fishsense-core's `WeightStore`. Core
names the models and pins each file's sha256 and size in its manifest; this
module only says where the bytes come from. MLflow can replace this store later
without touching core or the stages.

v2 changes:

* **nothing unverified loads.** v1 cached whatever the key held. `fetch` hashes
  the download against the manifest before moving it into the cache, so a
  tampered or truncated object is refused, and nothing is cached;
* the download streams to disk (`download_file`) instead of holding the whole
  checkpoint in memory, as v1's `_get` did;
* a key the bucket lacks is `ModelUnavailable`; any other S3 error (a 403, a
  500) propagates, as v1's `_exists` let it;
* the settings are the weights' own (`FISHSENSE_MODEL_WEIGHTS_*`), not a
  section of a shared object-store config, and the cache directory is required.

fishsense-core 4.1.0's manifest had no SAM 3.1 entry, so the head/tail stage
passed its own one-entry manifest from settings; core pins it since 4.2.0, and
every model here now resolves through core's built-in manifest (2026-10-23).

**Weights that cannot load are the worker's, not the frame's**
(`refuse_unloadable_weights`, 2026-10-24). Every stage loads its model inside
it, so bad settings, bytes that aren't the pinned file, an object missing
from the bucket, or no manifest entry fail the activity non-retryably as
``{model}{kind}`` (`fishsense_services_contracts.refusals`). Before, the
head/tail, slate and species stages each kept a copy of the mapping, and the
laser detector and the keypoint model had none: each frame re-downloaded and
re-hashed the checkpoint on every try, and the workflows that leave a
failing frame out charged the pod's fault to every frame of the mission.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fishsense_core.models import (
    Manifest,
    ModelIntegrityError,
    ModelRef,
    ModelUnavailable,
    fetch,
)
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.refusals import WEIGHTS_FAILURE_KINDS

__all__ = [
    "GarageWeightStore",
    "ModelWeightsSettings",
    "afetch_weights",
    "fetch_weights",
    "model_key",
    "refuse_unloadable_weights",
]

# botocore surfaces a missing key as one of these `Error.Code` values,
# depending on the call: HeadObject (404/NotFound) or GetObject (NoSuchKey).
# `download_file` heads first.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class ModelWeightsSettings(BaseSettings):
    """Where the weights are, from ``FISHSENSE_MODEL_WEIGHTS_*``.

    Constructed when a role that loads models starts, so a processor that
    cannot reach its weights fails to start rather than failing its first
    model load after taking the work.
    """

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_MODEL_WEIGHTS_")

    #: Garage's S3 endpoint, e.g. ``https://s3.e4e.ucsd.edu``.
    endpoint_url: str
    #: Garage accepts any region name but signs with it; v1's is ``garage``.
    region: str = "garage"
    access_key_id: str
    secret_access_key: SecretStr
    #: v1's production models bucket. Its own grant, read-only for the
    #: processor: weights are not tenant data, so they are not under
    #: ``tenants/{tenant_id}/`` (PLAN.md §9.11).
    bucket: str = "model-weights"
    #: Only for a models bucket shared with another deployment; it names the
    #: deployment, the way v1's ``models_prefix`` did.
    prefix: str = ""
    #: The ``{name}/{version}/{filename}`` cache. It must be on the volume the
    #: pod mounts, or every load downloads again; v1's default named a PVC
    #: v2's pods do not have, so there is none.
    cache_dir: Path

    @field_validator("endpoint_url")
    @classmethod
    def _is_a_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("endpoint_url must be an http(s) URL with a host")
        return value

    @field_validator("access_key_id", "bucket")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # An env var from an empty Secret key is set, but blank.
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("secret_access_key")
    @classmethod
    def _secret_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("cache_dir", mode="before")
    @classmethod
    def _cache_dir_named(cls, value):
        # Blank would become Path("."): multi-GB weights in the working directory.
        if not str(value).strip():
            raise ValueError("cache_dir must name the pod's cache volume")
        return value

    @field_validator("prefix")
    @classmethod
    def _no_surrounding_slashes(cls, value: str) -> str:
        return value.strip("/")


def model_key(name: str, version: str, filename: str, prefix: str | None = "") -> str:
    """The Garage key of a model file.

    ``version`` is part of the key on purpose: new weights are a new object,
    so a cached copy can never be silently the wrong one. There is no
    ``models/`` segment, because the weights have their own bucket and such a
    segment would restate its name in every key. Surrounding slashes are
    stripped from ``prefix``, since S3 treats a double slash as another key.
    """
    base = f"{name}/{version}/{filename}"
    prefix = (prefix or "").strip("/")
    return f"{prefix}/{base}" if prefix else base


class GarageWeightStore:  # pylint: disable=too-few-public-methods
    """fishsense-core's `WeightStore` over the Garage models bucket.

    It only produces bytes; `fetch` decides whether they are the pinned file.
    The boto3 client is injected, so tests pass a moto-backed one.
    """

    def __init__(self, s3, bucket: str, prefix: str = ""):
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix

    @classmethod
    def from_settings(cls, settings: ModelWeightsSettings) -> "GarageWeightStore":
        """A store on Garage: path-style addressing (it has no virtual-host
        bucket DNS), an explicit endpoint and region, SigV4 (v1's
        `build_s3_client`)."""
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key.get_secret_value(),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return cls(s3, settings.bucket, prefix=settings.prefix)

    def download_to(self, ref: ModelRef, dest: Path) -> None:
        """Write ``ref``'s object to ``dest``, streamed rather than held in
        memory. A key the bucket lacks is `ModelUnavailable`."""
        key = model_key(ref.name, ref.version, ref.filename, self.prefix)
        try:
            self.s3.download_file(Bucket=self.bucket, Key=key, Filename=str(dest))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in _NOT_FOUND_CODES:
                raise ModelUnavailable(
                    f"{ref.id}: not in s3://{self.bucket}/{key}"
                ) from exc
            raise


def fetch_weights(
    name: str,
    version: str | None = None,
    *,
    store: GarageWeightStore,
    cache_dir: Path,
    manifest: Manifest | None = None,
) -> Path:
    """A local path to the verified weights for ``(name, version)``, fetched
    from ``store`` on a cache miss. ``version=None`` is core's pinned default;
    ``manifest`` overrides core's (tests)."""
    return fetch(name, version, store=store, cache_dir=cache_dir, manifest=manifest)


async def afetch_weights(
    name: str,
    version: str | None = None,
    *,
    store: GarageWeightStore,
    cache_dir: Path,
    manifest: Manifest | None = None,
) -> Path:
    """`fetch_weights` for an async activity. The download and the hash run
    off the event loop: inline, gigabytes would starve the Temporal heartbeats
    that keep the activity alive. Concurrent callers still download once
    (core's `fetch` locks per file)."""
    return await asyncio.to_thread(
        fetch_weights,
        name,
        version,
        store=store,
        cache_dir=cache_dir,
        manifest=manifest,
    )


#: Why weights cannot load, by `WEIGHTS_FAILURE_KINDS`' name.
_WHY = {
    "SettingsInvalid": "{settings} is missing or invalid",
    "WeightsCorrupt": "the file in model-weights is not the pinned one",
    "WeightsUnavailable": "the file is not in model-weights",
    "NotInManifest": "the manifest has no entry for it",
    "LoadInvalid": "something it loads does not validate (its manifest entry "
    "or model description: check fishsense-core's version)",
}
if set(_WHY) != set(WEIGHTS_FAILURE_KINDS):  # one list, both sides
    raise ImportError("weights._WHY and refusals.WEIGHTS_FAILURE_KINDS disagree")


def _settings_classes() -> set[str]:
    """The names of every settings class loaded (a `BaseSettings` subclass,
    at any depth)."""
    names, pending = set(), [BaseSettings]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            names.add(sub.__name__)
            pending.append(sub)
    return names


def _kind(exc: BaseException) -> Optional[str]:
    """Which weights failure `exc` is, or None for one a retry might pass.

    * the pod's settings: a settings class's `ValidationError` (its `title`
      names the class), or pydantic-settings' `SettingsError` (a value it
      cannot parse);
    * `ModelIntegrityError`: not the pinned bytes;
    * `ModelUnavailable`: final too, since this store raises it only for a
      key the bucket lacks (`GarageWeightStore.download_to`), which a retry
      cannot add; any other S3 error (an outage, a 5xx, a 403) propagates as
      itself, and stays retryable;
    * core's manifest `KeyError` ("unknown model ..."). Any other `KeyError`
      -- a checkpoint's state dict, an environment lookup -- is not a missing
      entry, and propagates (review of #364);
    * any other `ValidationError`: deterministic, so final, and named for what
      it is rather than blamed on the settings (reviews of #358 and #364).
    """
    if isinstance(exc, SettingsError):
        return "SettingsInvalid"
    if isinstance(exc, ValidationError):
        return "SettingsInvalid" if exc.title in _settings_classes() else "LoadInvalid"
    if isinstance(exc, ModelIntegrityError):
        return "WeightsCorrupt"
    if isinstance(exc, ModelUnavailable):
        return "WeightsUnavailable"
    if (
        isinstance(exc, KeyError)
        and exc.args
        and str(exc.args[0]).startswith("unknown model ")
    ):
        return "NotInManifest"
    return None


@contextmanager
def refuse_unloadable_weights(
    model: str,
    label: str,
    *,
    settings: str = "FISHSENSE_MODEL_WEIGHTS_*",
) -> Iterator[None]:
    """Run a model's load; weights that cannot load leave it as a
    non-retryable `ApplicationError` of type ``{model}{kind}`` (``model`` is
    one of `fishsense_services_contracts.refusals`' names, ``kind`` one of its
    `WEIGHTS_FAILURE_KINDS`), saying which cause. ``label`` names the model
    in the message, ``settings`` the settings it reads. Anything else
    propagates unchanged."""
    try:
        yield
    except Exception as exc:  # pylint: disable=broad-except
        kind = _kind(exc)
        if kind is None:
            raise
        raise ApplicationError(
            f"{label}'s weights cannot load: "
            f"{_WHY[kind].format(settings=settings)}: {exc}",
            type=f"{model}{kind}",
            non_retryable=True,
        ) from exc
