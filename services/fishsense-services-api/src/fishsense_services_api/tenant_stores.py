"""Each tenant's objects: its own Garage bucket, under its own key.

PLAN.md §9.11: Garage grants keys per bucket, so a tenant prefix isolates
nothing. Bucket-per-tenant was due before the first partner upload, and a
mobile sync is that upload. Each tenant's bucket and key are provisioned
outside this repo (docs/handoff/krg-infra) and rendered into one JSON file,
``{tenant slug: {bucket, access_key_id, secret_access_key}}``. A tenant with
no entry has no store, and gets none: there is no shared fallback.

**Upload integrity** (§9.19, decided 2026-10-11). The API never sees the
bytes, so the client declares each file's SHA-256 and size up front, and the
presigned PUT signs ``x-amz-checksum-sha256`` and ``content-length``. Garage
then refuses any other bytes (400 ``InvalidDigest``) and refuses a PUT that
omits the header. Commit reads both back with HEAD (``ChecksumMode``).

Two endpoints: URLs are signed for the **public** one, which the phone and
the browser call (a signature covers the host); the API's own HEADs go to the
**internal** one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

__all__ = [
    "NoTenantStore",
    "PresignedUpload",
    "StoredObject",
    "TenantObjects",
    "TenantStore",
    "TenantStores",
]

#: How long a presigned upload or read stays valid.
DEFAULT_EXPIRES_SECONDS = 3600


class NoTenantStore(LookupError):
    """The tenant has no bucket configured, so it can store nothing yet."""


@dataclass(frozen=True)
class TenantStore:
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)


@dataclass(frozen=True)
class PresignedUpload:
    """A PUT the client makes, with exactly these headers."""

    url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class StoredObject:
    size: int
    #: Base64 SHA-256, as S3 reports it; None if stored without one.
    sha256: str | None


class TenantObjects:
    """One tenant's bucket."""

    def __init__(self, store: TenantStore, *, internal, public) -> None:
        self.bucket = store.bucket
        self._internal = internal
        self._public = public

    def __repr__(self) -> str:
        return f"TenantObjects(bucket={self.bucket!r})"

    def presign_put(
        self,
        key: str,
        *,
        sha256: str,
        size: int,
        content_type: str,
        expires_seconds: int = DEFAULT_EXPIRES_SECONDS,
    ) -> PresignedUpload:
        url = self._public.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ChecksumSHA256": sha256,
                "ContentLength": size,
                "ContentType": content_type,
            },
            ExpiresIn=expires_seconds,
        )
        headers = {
            "x-amz-checksum-sha256": sha256,
            "Content-Length": str(size),
            "Content-Type": content_type,
        }
        return PresignedUpload(url=url, headers=headers)

    def presign_get(
        self, key: str, *, expires_seconds: int = DEFAULT_EXPIRES_SECONDS
    ) -> str:
        return self._public.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def head(self, key: str) -> StoredObject | None:
        """What is stored at ``key``, or None if nothing is. Blocking."""
        try:
            found = self._internal.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                return None
            raise
        return StoredObject(
            size=found["ContentLength"], sha256=found.get("ChecksumSHA256")
        )


class TenantStores:
    def __init__(
        self,
        *,
        endpoint_url: str,
        public_endpoint_url: str,
        region: str,
        stores: Mapping[str, TenantStore],
    ) -> None:
        self._endpoint_url = endpoint_url
        self._public_endpoint_url = public_endpoint_url
        self._region = region
        self._stores = dict(stores)
        self._objects: dict[str, TenantObjects] = {}

    @classmethod
    def from_file(
        cls, path: Path, *, endpoint_url: str, public_endpoint_url: str, region: str
    ) -> TenantStores:
        rendered = json.loads(Path(path).read_text())
        return cls(
            endpoint_url=endpoint_url,
            public_endpoint_url=public_endpoint_url,
            region=region,
            stores={
                slug: TenantStore(
                    bucket=entry["bucket"],
                    access_key_id=entry["access_key_id"],
                    secret_access_key=entry["secret_access_key"],
                )
                for slug, entry in rendered.items()
            },
        )

    def __repr__(self) -> str:
        return f"TenantStores(tenants={sorted(self._stores)!r})"

    @property
    def tenants(self) -> set[str]:
        return set(self._stores)

    def for_tenant(self, slug: str) -> TenantObjects:
        objects = self._objects.get(slug)
        if objects is not None:
            return objects
        store = self._stores.get(slug)
        if store is None:
            raise NoTenantStore(slug)
        objects = self._objects[slug] = TenantObjects(
            store,
            internal=self._client(self._endpoint_url, store),
            public=self._client(self._public_endpoint_url, store),
        )
        return objects

    def _client(self, endpoint_url: str, store: TenantStore):
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=self._region,
            aws_access_key_id=store.access_key_id,
            aws_secret_access_key=store.secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                # Garage verifies only the checksum we sign; don't add a
                # second, client-computed one to our own HEAD/requests.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
