"""The processor's side of the object store (Garage, S3-compatible).

Ported from fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/src/
fishsense_data_processing_workflow_worker/object_store.py, and the primitives
it inherited from libs/fishsense-shared/src/fishsense_shared/object_store.py
(`build_s3_client`, `BaseObjectStoreClient._get/_put`). Model weights are
not here: `weights.GarageWeightStore` reads them, verified against
fishsense-core's manifest (PLAN.md §9.12).

v1's shape, kept: the processor reads staged raw frames and slate PDFs from
scratch and writes processed JPEGs to the labels bucket. It has **no NAS access and no way to delete anything** --
the orchestrator stages scratch in and cleans it up.

v2 changes:

* **it is handed an ``ObjectRef`` rather than a checksum.** Only the
  orchestrator issues keys (PLAN.md §9.11): it resolves where a frame was
  staged, where a JPEG v1 wrote still is, and where a new one goes
  (``tenants/{tenant_id}/...``). The processor never builds a key, so it cannot
  disagree with the layout;
* **it writes only a tenant's processed JPEG.** It runs on infrastructure we
  don't own, and a JPEG v1 wrote is what a Label Studio task shows a labeler,
  so an upload anywhere else is refused before it is sent;
* **raw frames and weights land in a file**, streamed to ``<name>.part`` and
  renamed, as the NAS client does: a raw frame is ~15 MB and SAM 3.1's
  weights are gigabytes, and a failed download leaves nothing half-written.
  Every read closes its StreamingBody, or botocore's pool drains (v1).

Every call is bounced through ``asyncio.to_thread``: boto3 is synchronous and
these run in activities on the event loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import boto3
from botocore.config import Config

from fishsense_services_contracts.object_store import (
    is_processed_jpeg_key,
    TENANTS_PREFIX,
    ObjectRef,
    ObjectStoreConnection,
)

__all__ = [
    "ProcessorObjectStore",
    "RefusedWrite",
    "build_s3_client",
]

# Big enough to keep syscalls few, small enough that a frame is never held.
_CHUNK_BYTES = 1024 * 1024


class RefusedWrite(ValueError):
    """The processor was asked to write something other than a tenant's
    processed JPEG."""


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


class ProcessorObjectStore:
    """Read scratch, write a tenant's JPEGs. Never delete."""

    def __init__(self, s3, settings: ObjectStoreConnection) -> None:
        self._s3 = s3
        self._labels_bucket = settings.labels_bucket
        self._legacy_prefix = settings.legacy_labels_prefix

    @classmethod
    def from_settings(cls, settings: ObjectStoreConnection) -> "ProcessorObjectStore":
        return cls(build_s3_client(settings), settings)

    # -- reads ---------------------------------------------------------------

    async def download_raw(self, ref: ObjectRef, directory: Path) -> Path:
        """The staged raw frame, as ``{directory}/{basename of its key}``."""
        return await self._get_to_file(ref, Path(directory) / _basename(ref.key))

    async def download_slate_pdf(self, ref: ObjectRef) -> bytes:
        return await self._get(ref)

    async def download_processed_jpeg(self, ref: ObjectRef) -> bytes:
        """A processed JPEG -- a tenant's, or one v1 wrote, as the orchestrator
        resolved it. Head/tail predict reads the stage-5.1 JPEG because it is
        the exact frame the labeler is shown (v1)."""
        return await self._get(ref)

    # -- writes --------------------------------------------------------------

    async def upload_processed_jpeg(self, ref: ObjectRef, data: bytes) -> None:
        self._check_jpeg_target(ref)
        await asyncio.to_thread(
            self._s3.put_object, Bucket=ref.bucket, Key=ref.key, Body=data
        )

    def _check_jpeg_target(self, ref: ObjectRef) -> None:
        if ref.bucket != self._labels_bucket or not is_processed_jpeg_key(
            ref.key, legacy_prefix=self._legacy_prefix
        ):
            raise RefusedWrite(
                f"the processor writes only a processed JPEG, "
                f"{{folder}}/{{checksum}}.JPG in s3://{self._labels_bucket}/ under "
                f"{TENANTS_PREFIX}/{{tenant uuid}}/ or v1's prefix, not {ref.uri}"
            )

    # -- primitives ----------------------------------------------------------

    async def _get(self, ref: ObjectRef) -> bytes:
        def _do() -> bytes:
            body = self._s3.get_object(Bucket=ref.bucket, Key=ref.key)["Body"]
            try:
                return body.read()
            finally:
                body.close()

        return await asyncio.to_thread(_do)

    async def _get_to_file(self, ref: ObjectRef, path: Path) -> Path:
        partial = path.with_name(path.name + ".part")

        def _do() -> Path:
            body = self._s3.get_object(Bucket=ref.bucket, Key=ref.key)["Body"]
            try:
                with open(partial, "wb") as handle:
                    for chunk in body.iter_chunks(_CHUNK_BYTES):
                        handle.write(chunk)
                os.replace(partial, path)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            finally:
                body.close()
            return path

        return await asyncio.to_thread(_do)


def _basename(key: str) -> str:
    return key.rsplit("/", 1)[-1]
