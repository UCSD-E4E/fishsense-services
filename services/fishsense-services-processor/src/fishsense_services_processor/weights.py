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

Not every model core serves can come from here yet: fishsense-core 4.1.0's
manifest has no SAM 3.1 entry, so `fetch("sam3", "3.1")` is a `KeyError` until
core pins its hash (see the head/tail stage).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fishsense_core.models import Manifest, ModelRef, ModelUnavailable, fetch
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "GarageWeightStore",
    "ModelWeightsSettings",
    "afetch_weights",
    "fetch_weights",
    "model_key",
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
        if not value.startswith(("https://", "http://")):
            raise ValueError("endpoint_url must be an http(s) URL")
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
    ``manifest`` overrides core's (tests, or a model core does not pin yet)."""
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
