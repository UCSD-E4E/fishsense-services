"""Slate template PDFs: staged from the NAS, and read back for their aspect.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/stage_slate_pdf_activity.py (the
staging) and sync_dive_slate_labels_for_label_studio_project_activity.py
(`compute_pdf_panel_aspect_ratio`, `compute_pdf_panel_width_in_composite`,
`_aspect_ratio_for_slate`).

v1's rules, kept:

* staging is idempotent -- a HEAD first, and a template already staged is not
  fetched again -- and the NAS is only ever read, never written;
* the template's NAS path is share-relative and is resolved against the raw
  root before it reaches FileStation (which reports an unresolved path as a
  502, so the failure would look transient);
* the composite's panel width is the PDF page's width/height in points times
  the composite's height: DPI cancels, so the page aspect is all it needs.

v2 changes:

* per tenant: the PDF is keyed `tenants/{tenant}/slate_pdf/{template}.pdf`
  (v1: `slate_pdf/{slate_id}.pdf`). **A migrated template is staged from
  v1's key** when its PDF is still there -- copied to the tenant's, v1's only
  read -- so neither the NAS nor a NAS path is needed for it (as the JPEG
  lookup reads v1's key for a migrated frame);
* a template that is missing, or has no NAS path (and no PDF v1 staged), is
  a final refusal (v1: a plain ValueError, retried until its timeout);
* **the sync stages a PDF it needs rather than failing on its absence.** v1
  read the PDF only from scratch and raised when it was missing, keeping the
  project's cursor put until a stage-9 run staged it. Every carried-over
  project's PDF lives at v1's key, not the tenant's, so after cutover each
  slate sync would fail until its dive happened to re-enter stage 9. The
  sync still raises when the PDF cannot be had at all -- composite-space
  geometry is never persisted.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pymupdf
from botocore.exceptions import ClientError
from synology_filestation import FileStationError
from temporalio import activity

from fishsense_services_api.slate_store import SlateTemplate
from fishsense_services_orchestrator.ingest.nas import NasClient
from fishsense_services_orchestrator.ingest.nas_errors import (
    raise_if_permanent_dsm_error,
)
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
    resolve_nas_path,
)
from fishsense_services_orchestrator.object_store.store import (
    NOT_FOUND_CODES,
    OrchestratorObjectStore,
)

__all__ = [
    "SlatePdfUnavailable",
    "SlatePdfs",
    "compute_pdf_panel_aspect_ratio",
    "compute_pdf_panel_width_in_composite",
]


class SlatePdfUnavailable(ValueError):
    """The template cannot be staged: it is unknown, or has no NAS path."""


class _Templates(Protocol):
    async def slate_template(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> SlateTemplate | None: ...


def compute_pdf_panel_aspect_ratio(pdf_bytes: bytes) -> float:
    """Read page 0 of a slate PDF and return its width / height in points.

    The composite scales the page to the photo's height, so the panel's width
    is `pdf_width * scale = (pdf_width / pdf_height) * original_height`.
    pymupdf reports `page.rect` in DPI-independent points (1/72 inch), so the
    ratio is the only intrinsic needed; DPI cancels out of the offset.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        page: pymupdf.Page = document.load_page(0)
        rect = page.rect
        return float(rect.width) / float(rect.height)


def compute_pdf_panel_width_in_composite(
    pdf_aspect_ratio: float, original_height: float
) -> float:
    """Pixel width of the PDF panel inside the Label Studio composite image."""
    return pdf_aspect_ratio * float(original_height)


class SlatePdfs:
    """Stage a tenant's slate PDFs, and read their page aspect back."""

    def __init__(
        self,
        *,
        catalog: _Templates,
        store: OrchestratorObjectStore,
        nas_settings: NasSettings,
        nas_client_factory: Callable[[], NasClient] | None = None,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._nas_settings = nas_settings
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    async def stage(self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID) -> bool:
        """Put the template's PDF in the tenant's scratch unless it is there.
        True once it is in the object store."""
        template = await self._catalog.slate_template(tenant_id, slate_template_id)
        if template is None:
            raise SlatePdfUnavailable(
                f"slate_template_id={slate_template_id} not found"
            )

        if await self._store.has_slate_pdf(tenant_id, slate_template_id):
            activity.logger.info(
                "slate template %s already staged for tenant %s; skipping the NAS",
                slate_template_id,
                tenant_id,
            )
            return True
        if template.v1_id is not None:
            data = await self._store.download_legacy_slate_pdf(template.v1_id)
            if data is not None:
                await self._store.upload_slate_pdf(tenant_id, slate_template_id, data)
                activity.logger.info(
                    "staged slate template %s for tenant %s from v1's slate_pdf/%d",
                    slate_template_id,
                    tenant_id,
                    template.v1_id,
                )
                return True

        if not template.source_path:
            raise SlatePdfUnavailable(
                f"slate_template_id={slate_template_id} has no NAS path"
            )

        nas = self._nas_client_factory()
        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = resolve_nas_path(template.source_path, self._nas_settings)
            try:
                await asyncio.to_thread(
                    nas.download_to, src_path=src_path, dest_dir=tmpdir
                )
            except FileStationError as exc:
                raise_if_permanent_dsm_error(exc, context=src_path)
                raise
            data = await asyncio.to_thread(
                (Path(tmpdir) / Path(src_path).name).read_bytes
            )
            await self._store.upload_slate_pdf(tenant_id, slate_template_id, data)
        activity.logger.info(
            "staged slate template %s for tenant %s", slate_template_id, tenant_id
        )
        return True

    async def aspect(self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID) -> float:
        """The template page's width/height, staging the PDF first if it is
        not in scratch (see the module docstring)."""
        try:
            pdf = await self._store.download_slate_pdf(tenant_id, slate_template_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") not in NOT_FOUND_CODES:
                raise
            activity.logger.info(
                "slate template %s is not staged for tenant %s; staging it for "
                "the sync",
                slate_template_id,
                tenant_id,
            )
            await self.stage(tenant_id, slate_template_id)
            pdf = await self._store.download_slate_pdf(tenant_id, slate_template_id)
        return await asyncio.to_thread(compute_pdf_panel_aspect_ratio, pdf)
