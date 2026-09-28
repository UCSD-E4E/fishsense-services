"""The object store (Garage, S3-compatible): how both sides reach it, and what
the orchestrator hands the processor to read or write.

Ported in shape from fishsense-lite@77e8f8e5
libs/fishsense-shared/src/fishsense_shared/object_store.py (`open_client`'s
settings and their defaults). v1 put the key contract here too, because both
workers built keys; v2 does not. **Only the orchestrator issues keys** (PLAN.md
§9.11): it resolves where an object is, and the processor is handed an
``ObjectRef`` -- a bucket and a key -- and reads or writes exactly that. The
layout (``tenants/{tenant_id}/...``) therefore lives with the orchestrator,
and the processor cannot drift from it by building a key of its own.

The settings live here, like ``TemporalConnection``, because both sides must
point at the same Garage and the same buckets. From
``FISHSENSE_OBJECT_STORE_*``, validated at startup.

The buckets are v1's (production: ``fishsense-lite`` scratch,
``labels-fishsense-lite`` for the JPEGs Label Studio serves, ``model-weights``).
Each is shared by every tenant, with the tenant as the first key segment
(§9.11); a later move to bucket-per-tenant swaps that segment for a bucket.
"""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["TENANTS_PREFIX", "ObjectRef", "ObjectStoreConnection"]

#: The first segment of every key v2 writes: ``tenants/{tenant_id}/...``.
TENANTS_PREFIX = "tenants"


class ObjectStoreConnection(BaseSettings):
    """Garage, from ``FISHSENSE_OBJECT_STORE_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_OBJECT_STORE_")

    #: http(s), host and optional port, e.g. ``https://s3.e4e.ucsd.edu``.
    endpoint_url: str
    #: Garage's region name (production: ``garage``); SigV4 signs with it.
    region: str
    access_key_id: str
    secret_access_key: SecretStr
    #: Scratch: raw ``.ORF`` and slate PDFs, staged per dive and deleted after.
    bucket: str
    #: The processed JPEGs Label Studio serves. Defaults to ``bucket``.
    labels_bucket: str | None = None
    #: Model weights (PLAN.md §9.12). Defaults to ``bucket``.
    models_bucket: str | None = None
    #: Partitions ``models_bucket`` if it is shared with another project (v1's).
    models_prefix: str | None = ""
    #: Where v1 wrote its JPEGs inside ``labels_bucket`` (v1's ``labels_prefix``;
    #: production ``fishsense-lite``). Read-only: v2 writes under
    #: ``tenants/``, and this only says where v1's JPEGs already are.
    legacy_labels_prefix: str | None = ""

    @field_validator("endpoint_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        # Not a strict URL type: v1 learnt that compose hostnames (`garage`)
        # have no TLD, and a URL type that normalises adds a trailing slash.
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"endpoint_url must be an http(s) URL, got {value!r}")
        return value.rstrip("/")

    @field_validator("region", "access_key_id", "bucket")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("models_prefix", "legacy_labels_prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str:
        # None is "", never "None" in a key; stray slashes would name a
        # different object (`a//b` is not `a/b` to S3).
        return (value or "").strip("/")

    @model_validator(mode="after")
    def _defaults_and_namespaces(self) -> "ObjectStoreConnection":
        self.labels_bucket = self.labels_bucket or self.bucket
        self.models_bucket = self.models_bucket or self.bucket
        first = (self.legacy_labels_prefix or "").split("/", 1)[0]
        if first == TENANTS_PREFIX:
            raise ValueError(
                f"legacy_labels_prefix must not be under {TENANTS_PREFIX!r}/, "
                "where v2 keys its tenants' objects"
            )
        return self


class ObjectRef(BaseModel):
    """One object: where the orchestrator says it is, or is to be written."""

    model_config = ConfigDict(frozen=True)

    bucket: str
    key: str

    @field_validator("bucket")
    @classmethod
    def _bucket(cls, value: str) -> str:
        if not value:
            raise ValueError("bucket must not be empty")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        # An empty segment -- a leading, doubled or trailing slash -- names a
        # different object from the one meant.
        if not value or any(not segment for segment in value.split("/")):
            raise ValueError(f"not a well-formed object key: {value!r}")
        return value

    @property
    def uri(self) -> str:
        """``s3://bucket/key``: how Label Studio tasks name an image."""
        return f"s3://{self.bucket}/{self.key}"
