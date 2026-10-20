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
import logging
import re
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

logger = logging.getLogger(__name__)

#: How long a presigned upload or read stays valid.
DEFAULT_EXPIRES_SECONDS = 3600
#: A render's presigned PUT or GET rides in a processor payload, which can wait
#: in a queue for hours: a week, SigV4's longest (2026-10-19).
RENDER_EXPIRES_SECONDS = 7 * 24 * 3600


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
    #: Hex MD5 -- the ETag of an object PUT in one part (a render); None for
    #: a multipart ETag, which is not one.
    md5: str | None = field(default=None, compare=False)


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

    def presign_render_put(
        self, key: str, *, expires_seconds: int = RENDER_EXPIRES_SECONDS
    ) -> PresignedUpload:
        """A PUT of a render: its bytes are not known yet, so no checksum or
        size is signed -- the uploader reports the MD5, checked with `head`
        (2026-10-19)."""
        url = self._public.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": "image/jpeg"},
            ExpiresIn=expires_seconds,
        )
        return PresignedUpload(url=url, headers={"Content-Type": "image/jpeg"})

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
        etag = found.get("ETag", "").strip('"')
        return StoredObject(
            size=found["ContentLength"],
            sha256=found.get("ChecksumSHA256"),
            md5=etag if re.fullmatch(r"[0-9a-f]{32}", etag) else None,
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
        #: The rendered file, when the stores came from one, and the stat it
        #: was last read at.
        self._path: Path | None = None
        self._seen: tuple[int, int, int] | None = None

    @classmethod
    def from_file(
        cls, path: Path, *, endpoint_url: str, public_endpoint_url: str, region: str
    ) -> TenantStores:
        """The stores in the rendered file. They are optional: a file that is
        missing (not rendered yet), empty (not seeded), malformed, or holds an
        incomplete entry costs only the stores it can't give -- logged, never
        raised -- so it can stop mobile sync but never the API.

        The file is re-read whenever it changes: vault-agent re-renders it as
        krg-infra seeds or rotates a store, and a store seeded after startup
        must not wait for a restart (review of v0.6.1)."""
        path = Path(path)
        seen = _stat(path)
        stores = cls(
            endpoint_url=endpoint_url,
            public_endpoint_url=public_endpoint_url,
            region=region,
            stores=_read_stores(path),
        )
        stores._path, stores._seen = path, seen
        return stores

    def _reload_if_changed(self) -> None:
        if self._path is None:
            return
        seen = _stat(self._path)
        if seen == self._seen:
            return
        self._stores = _read_stores(self._path)
        self._objects = {}  # their keys may have rotated
        self._seen = seen
        logger.info(
            "tenant stores re-read from %s: %s", self._path, sorted(self._stores)
        )

    def __repr__(self) -> str:
        return f"TenantStores(tenants={sorted(self._stores)!r})"

    @property
    def tenants(self) -> set[str]:
        self._reload_if_changed()
        return set(self._stores)

    def for_tenant(self, slug: str) -> TenantObjects:
        self._reload_if_changed()
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


def _stat(path: Path) -> tuple[int, int, int] | None:
    """What changes when the file is rewritten, or None if it isn't there."""
    try:
        found = path.stat()
    except OSError:
        return None
    return (found.st_ino, found.st_mtime_ns, found.st_size)


def _read_stores(path: Path) -> dict[str, TenantStore]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        logger.warning("no tenant stores at %s yet: no tenant can sync", path)
        return {}
    if not text.strip():
        return {}  # the soft render, not seeded yet
    try:
        rendered = json.loads(text)
        if not isinstance(rendered, dict):
            raise ValueError("not an object")
    except ValueError as error:
        logger.error(
            "tenant stores at %s are not a JSON object (%s): no tenant can sync",
            path,
            error,
        )
        return {}
    stores = {}
    for slug, entry in rendered.items():
        try:
            stores[slug] = TenantStore(
                bucket=entry["bucket"],
                access_key_id=entry["access_key_id"],
                secret_access_key=entry["secret_access_key"],
            )
        except (KeyError, TypeError):
            logger.error(
                "tenant store %r in %s needs bucket, access_key_id and"
                " secret_access_key: %r can't sync",
                slug,
                path,
                slug,
            )
    return stores
