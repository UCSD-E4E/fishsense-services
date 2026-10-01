"""Stage 9's orchestrator activities: select, resolve, stage the PDF, clear.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(select_next_high_priority_dive_for_slate_preprocessing_activity.py,
resolve_slate_preprocess_inputs_activity.py, stage_slate_pdf_activity.py,
clear_slate_reprocess_flags_activity.py). v1's were SDK calls and client-side
filters; v2's call the slate catalog (`fishsense_services_api.slate_store`),
which owns the cohort and the frame selection.

v2 changes:

* the selector takes the oldest candidate across every tenant served;
* the resolver hands the processor refs the orchestrator issued: each
  frame's staged raw key, the key its composite is written to (over the JPEG
  where it already is -- v1's key for a migrated frame -- else the tenant's),
  and the tenant's slate PDF key; and it returns the checksums the flags are
  scoped to after the run;
* a dive that cannot be resolved, or a template that cannot be staged, is a
  final refusal: retrying reads the same rows to the same answer. The cohort
  no longer offers the ones knowable from the database (no camera
  calibration; a template with no dpi, reference points or NAS path), so
  they cannot be the oldest candidate every hour. A PDF the NAS reports
  missing still fails the run each hour until it is restored.
"""

from __future__ import annotations

import uuid
from typing import Optional, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.slate_store import (
    SlateInputsUnavailable,
    SlatePreprocessCandidate,
    SlatePreprocessInputs,
)
from fishsense_services_contracts.object_store import SLATE_JPEG_FOLDER
from fishsense_services_contracts.slate_calibration import (
    PreprocessSlateImage,
    PreprocessSlateImagesInput,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.slates.contracts import (
    ClearSlateFlagsInput,
    SlatePdfTarget,
    SlatePreprocessPlan,
)
from fishsense_services_orchestrator.slates.pdfs import (
    SlatePdfs,
    SlatePdfUnavailable,
)

__all__ = ["SlateActivities", "SlateCatalog"]


class SlateCatalog(Protocol):
    """What stage 9 asks the database; see
    ``fishsense_services_api.slate_store.SlateCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_slate_preprocessing(
        self, tenant_id: uuid.UUID
    ) -> SlatePreprocessCandidate | None: ...

    async def slate_preprocess_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SlatePreprocessInputs: ...

    async def clear_slate_reprocess_flags(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, checksums: list[str] | None
    ) -> int: ...


class SlateActivities:
    def __init__(
        self, *, catalog: SlateCatalog, store: OrchestratorObjectStore, pdfs: SlatePdfs
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._pdfs = pdfs

    @activity.defn(name="select_next_dive_for_slate_preprocessing")
    async def select_next_dive_for_slate_preprocessing(
        self,
    ) -> Optional[StagingTarget]:
        """The oldest dive in the stage-9 cohort, across tenants."""
        best: StagingTarget | None = None
        best_key = None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_slate_preprocessing(tenant_id)
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = StagingTarget(tenant_id, candidate.dive_id), key
        if best is None:
            activity.logger.info("no high-priority dives needing slate preprocessing")
        else:
            activity.logger.info(
                "next high-priority dive needing slate preprocessing: "
                "tenant=%s dive=%s",
                best.tenant_id,
                best.dive_id,
            )
        return best

    @activity.defn(name="resolve_slate_preprocess_inputs")
    async def resolve_slate_preprocess_inputs(
        self, target: StagingTarget
    ) -> SlatePreprocessPlan:
        try:
            inputs = await self._catalog.slate_preprocess_inputs(
                target.tenant_id, target.dive_id
            )
        except SlateInputsUnavailable as exc:
            raise ApplicationError(
                f"cannot resolve stage 9 for dive {target.dive_id}: {exc}",
                type="SlateInputsUnavailable",
                non_retryable=True,
            ) from exc

        layout = self._store.layout
        images = []
        for capture in inputs.captures:
            images.append(
                PreprocessSlateImage(
                    capture_id=capture.capture_id,
                    raw=layout.raw(target.tenant_id, capture.checksum),
                    jpeg=await self._store.processed_jpeg_target(
                        target.tenant_id,
                        SLATE_JPEG_FOLDER,
                        capture.checksum,
                        from_v1=capture.from_v1,
                    ),
                )
            )
            activity.heartbeat()
        template = inputs.slate_template
        activity.logger.info(
            "resolved slate preprocess inputs dive=%s slate=%s images=%d",
            target.dive_id,
            template.id,
            len(images),
        )
        return SlatePreprocessPlan(
            payload=PreprocessSlateImagesInput(
                dive_id=target.dive_id,
                slate_template_id=template.id,
                slate_pdf=layout.slate_pdf(target.tenant_id, template.id),
                slate_dpi=template.dpi,
                reference_points=template.reference_points,
                camera_matrix=inputs.camera_matrix,
                distortion_coefficients=inputs.distortion_coefficients,
                images=images,
            ),
            checksums=[capture.checksum for capture in inputs.captures],
        )

    @activity.defn(name="stage_slate_pdf")
    async def stage_slate_pdf(self, target: SlatePdfTarget) -> bool:
        """Stage the template's PDF from the NAS unless it is already there."""
        try:
            return await self._pdfs.stage(target.tenant_id, target.slate_template_id)
        except SlatePdfUnavailable as exc:
            raise ApplicationError(
                str(exc), type="SlatePdfUnavailable", non_retryable=True
            ) from exc

    @activity.defn(name="clear_slate_reprocess_flags")
    async def clear_slate_reprocess_flags(self, payload: ClearSlateFlagsInput) -> int:
        """Lower the dive's stage-9 redraw flags, scoped as the parent says."""
        cleared = await self._catalog.clear_slate_reprocess_flags(
            payload.tenant_id, payload.dive_id, checksums=payload.checksums
        )
        activity.logger.info(
            "cleared slate reprocess flags dive=%s rows=%d scope=%s",
            payload.dive_id,
            cleared,
            "whole dive" if payload.checksums is None else len(payload.checksums),
        )
        return cleared
