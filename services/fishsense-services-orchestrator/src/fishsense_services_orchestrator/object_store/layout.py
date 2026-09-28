"""Where every object lives: the keys the orchestrator issues.

Ported from fishsense-lite@77e8f8e5
libs/fishsense-shared/src/fishsense_shared/object_store.py (`raw_key`,
`slate_pdf_key`, `jpeg_key` and the per-stage JPEG folders). v1 shared these
between its two workers; in v2 **only the orchestrator issues keys** (PLAN.md
§9.11) and hands the processor an ``ObjectRef``, so the layout lives here.

v1's layout, kept below the tenant::

    raw/{checksum}.ORF                 scratch bucket; staged, read, deleted
    slate_pdf/{slate}.pdf              scratch bucket
    {folder}/{checksum}.JPG            labels bucket; Label Studio presigns it

Scratch keys carry a content-type segment because ``raw/`` and ``slate_pdf/``
share a bucket; JPEGs have their own bucket, so theirs would only restate it.

v2 changes:

* **every new key is under ``tenants/{tenant_id}/``** (§9.11, decided
  2026-09-27). One shared bucket per purpose, isolation enforced here, and the
  rest of the key independent of the tenant -- so moving to bucket-per-tenant
  swaps ``tenants/{tenant_id}/`` for a bucket in ``_place`` and nothing else;
* **the JPEGs v1 already wrote stay where they are**
  (``{labels_prefix}/{folder}/{checksum}.JPG``), because Label Studio tasks and
  label ``image_url``s point at them (§6.4: objects are not moved at cutover).
  ``processed_jpeg_candidates`` is the legacy key resolver: a frame migrated
  from v1 resolves to its new key, then v1's; a frame v2 ingested never
  resolves to a v1 key, because v1's keys carry no tenant;
* a slate PDF is keyed by its template's uuid (v1: the integer id). The PDFs
  v1 staged stay where they are, and a migrated template's is read from
  there (``legacy_slate_pdf``) instead of being fetched from the NAS again.
"""

from __future__ import annotations

import uuid

from fishsense_services_contracts.object_store import (
    CHECKERBOARD_LATTICE_JPEG_FOLDER,
    HEADTAIL_JPEG_FOLDER,
    JPEG_FOLDERS,
    LASER_JPEG_FOLDER,
    ObjectRef,
    ObjectStoreConnection,
    SLATE_JPEG_FOLDER,
    SPECIES_JPEG_FOLDER,
    TENANTS_PREFIX,
)

__all__ = [
    "CHECKERBOARD_LATTICE_JPEG_FOLDER",
    "HEADTAIL_JPEG_FOLDER",
    "JPEG_FOLDERS",
    "LASER_JPEG_FOLDER",
    "RAW_PREFIX",
    "SLATE_JPEG_FOLDER",
    "SLATE_PDF_PREFIX",
    "SPECIES_JPEG_FOLDER",
    "ObjectLayout",
]

RAW_PREFIX = "raw"
SLATE_PDF_PREFIX = "slate_pdf"


class ObjectLayout:
    """The buckets, and the keys within them, for every object v2 touches."""

    def __init__(self, settings: ObjectStoreConnection) -> None:
        self._scratch = settings.bucket
        self._labels = settings.labels_bucket
        self._legacy_labels_prefix = settings.legacy_labels_prefix

    @staticmethod
    def tenant_prefix(tenant_id: uuid.UUID) -> str:
        """``tenants/{tenant_id}``: the first segment of every new key."""
        return f"{TENANTS_PREFIX}/{uuid.UUID(str(tenant_id))}"

    def raw(self, tenant_id: uuid.UUID, checksum: str) -> ObjectRef:
        """A staged raw frame (scratch)."""
        return self._place(
            tenant_id, self._scratch, f"{RAW_PREFIX}/{_checked(checksum)}.ORF"
        )

    def slate_pdf(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> ObjectRef:
        """A staged slate template PDF (scratch)."""
        slate = uuid.UUID(str(slate_template_id))
        return self._place(tenant_id, self._scratch, f"{SLATE_PDF_PREFIX}/{slate}.pdf")

    def legacy_slate_pdf(self, v1_id: int) -> ObjectRef:
        """Where v1 staged a migrated template's PDF: v1's `slate_pdf_key`, in
        the scratch bucket by v1's integer id, with no prefix (v1's scratch
        keys never took one). Read-only, like every v1 key."""
        return ObjectRef(
            bucket=self._scratch, key=f"{SLATE_PDF_PREFIX}/{int(v1_id)}.pdf"
        )

    def processed_jpeg(
        self, tenant_id: uuid.UUID, folder: str, checksum: str
    ) -> ObjectRef:
        """Where a processed JPEG is **written**: always the tenant's key."""
        return self._place(
            tenant_id, self._labels, f"{_folder(folder)}/{_checked(checksum)}.JPG"
        )

    def legacy_processed_jpeg(self, folder: str, checksum: str) -> ObjectRef:
        """Where v1 wrote this JPEG: v1's `jpeg_key`, in the labels bucket.
        Read-only -- nothing in v2 writes a key without a tenant."""
        base = f"{_folder(folder)}/{_checked(checksum)}.JPG"
        prefix = self._legacy_labels_prefix
        return ObjectRef(
            bucket=self._labels, key=f"{prefix}/{base}" if prefix else base
        )

    def processed_jpeg_candidates(
        self, tenant_id: uuid.UUID, folder: str, checksum: str, *, from_v1: bool
    ) -> list[ObjectRef]:
        """Where a processed JPEG may be, in the order to look.

        ``from_v1`` is whether the frame was migrated from v1 (its capture has a
        ``v1_id``). Only then may a v1 key answer: v1's keys carry no tenant,
        and v1's frames are all the lab tenant's.
        """
        # v1's key first: v1 overwrote a frame's JPEG in place, so existing
        # Label Studio tasks and label image_urls point at it, and populate
        # dedupes by URL. A migrated frame keeps that key for good; a redraw
        # overwrites it where it is (`OrchestratorObjectStore.processed_jpeg_target`).
        new = self.processed_jpeg(tenant_id, folder, checksum)
        if from_v1:
            return [self.legacy_processed_jpeg(folder, checksum), new]
        return [new]

    def _place(self, tenant_id: uuid.UUID, bucket: str, relative: str) -> ObjectRef:
        # The one place a tenant becomes a location. Bucket-per-tenant (§9.11)
        # changes this to pick the tenant's bucket and drop the prefix.
        return ObjectRef(
            bucket=bucket, key=f"{self.tenant_prefix(tenant_id)}/{relative}"
        )


def _folder(folder: str) -> str:
    if folder not in JPEG_FOLDERS:
        raise ValueError(f"unknown JPEG folder {folder!r}; one of {JPEG_FOLDERS}")
    return folder


def _checked(checksum: str) -> str:
    if not checksum or not checksum.isalnum():
        raise ValueError(f"not a checksum: {checksum!r}")
    return checksum
