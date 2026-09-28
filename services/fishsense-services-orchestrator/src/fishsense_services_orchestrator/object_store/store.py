"""The orchestrator's object-store client: stage scratch in, check for the
processor's JPEGs, clean scratch up.

Ported from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
object_store.py (the api-worker's vocabulary: `has_raw`, `upload_raw`,
`delete_raw`, the slate PDF trio, `has_processed_jpeg`) and the primitives it
inherited from libs/fishsense-shared/src/fishsense_shared/object_store.py
(`build_s3_client`, `BaseObjectStoreClient._exists/_get/_put/_delete`).

v1's rules, kept:

* **only a not-found is "absent"** (`NOT_FOUND_CODES`): any other error
  propagates, or staging would re-upload on every firing and cleanup would
  think it had nothing to delete;
* every GET closes its StreamingBody, or botocore's pool drains;
* the orchestrator never writes a JPEG -- the processor does -- and the only
  deletes it can issue are of scratch. **NAS safety**: nothing here can touch
  the NAS.

v2 changes: keys come from the `ObjectLayout` (every one under
``tenants/{tenant_id}/``), and the JPEG check is the legacy key resolver's:
`locate_processed_jpeg` says *where* the JPEG is -- the tenant's key, or for a
frame migrated from v1 the key v1 wrote -- because populate needs that
location for the task it creates, not only the yes/no v1's gate needed.

The primitives are a second copy of the processor's (its
``object_store`` module): the contract package carries no boto3, and neither
service may import the other. Both are pinned by the same ported tests.
"""

from __future__ import annotations

import asyncio
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

__all__ = ["NOT_FOUND_CODES", "OrchestratorObjectStore", "build_s3_client"]

# botocore reports a missing key as one of these, depending on whether the call
# was HeadObject (404/NotFound) or GetObject (NoSuchKey).
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def build_s3_client(settings: ObjectStoreConnection):
    """A boto3 S3 client pointed at Garage: **path-style** addressing (Garage
    has no virtual-host bucket DNS), an explicit endpoint and region, SigV4."""
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name=settings.region,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key.get_secret_value(),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


class OrchestratorObjectStore:
    """Stage scratch in (HEAD + PUT), find JPEGs (HEAD), clean scratch (DELETE).

    Every call is bounced through ``asyncio.to_thread``: boto3 is synchronous
    and these run in activities on the event loop.
    """

    def __init__(self, s3, layout: ObjectLayout) -> None:
        self._s3 = s3
        self.layout = layout

    @classmethod
    def from_settings(
        cls, settings: ObjectStoreConnection
    ) -> "OrchestratorObjectStore":
        return cls(build_s3_client(settings), ObjectLayout(settings))

    # -- staging in ----------------------------------------------------------

    async def has_raw(self, tenant_id: uuid.UUID, checksum: str) -> bool:
        return await self._exists(self.layout.raw(tenant_id, checksum))

    async def upload_raw(
        self, tenant_id: uuid.UUID, checksum: str, data: bytes
    ) -> None:
        await self._put(self.layout.raw(tenant_id, checksum), data)

    async def has_slate_pdf(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> bool:
        return await self._exists(self.layout.slate_pdf(tenant_id, slate_template_id))

    async def upload_slate_pdf(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID, data: bytes
    ) -> None:
        await self._put(self.layout.slate_pdf(tenant_id, slate_template_id), data)

    async def download_slate_pdf(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> bytes:
        """A staged slate PDF, read back: the dive-slate sync and populate need
        the template's aspect ratio (v1)."""
        return await self._get(self.layout.slate_pdf(tenant_id, slate_template_id))

    # -- the processor's JPEGs (read-only) -----------------------------------

    async def locate_processed_jpeg(
        self, tenant_id: uuid.UUID, folder: str, checksum: str, *, from_v1: bool
    ) -> ObjectRef | None:
        """Where the processed JPEG is, or None if it isn't written yet.

        The tenant's key first, then -- for a frame migrated from v1 only --
        the key v1 wrote, which Label Studio tasks already point at. A
        decoupled populate must never seed a task for a frame whose JPEG isn't
        written: the dive would drop out of the preprocess cohort with a broken
        image (v1).
        """
        for ref in self.layout.processed_jpeg_candidates(
            tenant_id, folder, checksum, from_v1=from_v1
        ):
            if await self._exists(ref):
                return ref
        return None

    async def has_processed_jpeg(
        self, tenant_id: uuid.UUID, folder: str, checksum: str, *, from_v1: bool
    ) -> bool:
        return (
            await self.locate_processed_jpeg(
                tenant_id, folder, checksum, from_v1=from_v1
            )
            is not None
        )

    # -- scratch cleanup (Garage only -- NEVER the NAS) ----------------------

    async def delete_raw(self, tenant_id: uuid.UUID, checksum: str) -> bool:
        """Delete the staged scratch copy. True always: a delete is idempotent.
        The NAS source is never touched."""
        await self._delete(self.layout.raw(tenant_id, checksum))
        return True

    # -- primitives ----------------------------------------------------------

    async def _exists(self, ref: ObjectRef) -> bool:
        """HeadObject, mapping only a not-found to False."""

        def _do() -> bool:
            try:
                self._s3.head_object(Bucket=ref.bucket, Key=ref.key)
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code", "") in NOT_FOUND_CODES:
                    return False
                raise

        return await asyncio.to_thread(_do)

    async def _get(self, ref: ObjectRef) -> bytes:
        def _do() -> bytes:
            body = self._s3.get_object(Bucket=ref.bucket, Key=ref.key)["Body"]
            try:
                return body.read()
            finally:
                body.close()

        return await asyncio.to_thread(_do)

    async def _put(self, ref: ObjectRef, data: bytes) -> None:
        await asyncio.to_thread(
            self._s3.put_object, Bucket=ref.bucket, Key=ref.key, Body=data
        )

    async def _delete(self, ref: ObjectRef) -> None:
        # delete_object on an absent key succeeds, so retries are safe.
        await asyncio.to_thread(self._s3.delete_object, Bucket=ref.bucket, Key=ref.key)
