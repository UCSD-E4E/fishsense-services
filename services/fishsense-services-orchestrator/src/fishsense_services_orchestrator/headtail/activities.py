"""The head/tail stages' orchestrator activities: select, resolve, clear, persist.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/:
select_next_high_priority_dive_for_headtail_preprocessing_activity.py,
resolve_headtail_preprocess_inputs_activity.py,
clear_headtail_reprocess_flags_activity.py (and reprocess_scope.py's
`ClearReprocessFlagsInput`),
select_next_high_priority_dive_for_headtail_prediction_activity.py,
resolve_headtail_predict_inputs_activity.py and
persist_headtail_predictions_activity.py. v1's were SDK calls plus client-side
filtering; v2's call the head/tail catalog
(`fishsense_services_api.headtail_store`), which owns the cohorts and the
resolvers' predicates, and the object store, which says where every JPEG is.

v2 changes:

* the target is (tenant, dive), and each selector takes the best candidate
  across every tenant the orchestrator serves: the oldest, and for
  prediction never-predicted work first, as v1 ordered its one tenant;
* **the orchestrator issues the keys** (PLAN.md §9.11): a frame's raw ref is
  its tenant's scratch key; its JPEG is written over the one that exists (v1's
  key for a migrated frame, as v1 overwrote in place) or under the tenant; the
  predict resolver hands over the ref it located;
* a refusal of the processor's predictions is final (non-retryable), and a
  worker status (`skipped_no_upgrade_available`) is refused, never written.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.headtail_store import (
    HeadTailPredictionRow,
    HeadtailCandidate,
    HeadtailPreprocessInputs,
    InvalidPredictions,
    PredictCapture,
    PredictionCandidate,
)
from fishsense_services_contracts.headtail import (
    HEADTAIL_PREDICTOR_VERSION,
    HEADTAIL_STATUSES,
    HeadtailPredictionResult,
    PredictHeadtailImage,
    PredictHeadtailImagesInput,
    PreprocessHeadtailImage,
    PreprocessHeadtailImagesInput,
)
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

__all__ = [
    "ClearHeadtailReprocessFlags",
    "HeadtailActivities",
    "HeadtailCatalog",
    "HeadtailTarget",
]


@dataclass(frozen=True)
class HeadtailTarget:
    """A dive of a tenant. Serialises as the object store's `StagingTarget`."""

    tenant_id: uuid.UUID
    dive_id: uuid.UUID


@dataclass(frozen=True)
class ClearHeadtailReprocessFlags:
    """A dive, and the frames whose flags may come down (v1's
    `ClearReprocessFlagsInput`). `checksums=None` is the whole dive -- the
    no-work backstop; a list, empty included, is only those frames."""

    tenant_id: uuid.UUID
    dive_id: uuid.UUID
    checksums: Optional[List[str]] = None


class HeadtailCatalog(Protocol):
    """See ``fishsense_services_api.headtail_store.HeadtailCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_headtail_preprocessing(
        self, tenant_id: uuid.UUID
    ) -> HeadtailCandidate | None: ...

    async def headtail_preprocess_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> HeadtailPreprocessInputs: ...

    async def clear_headtail_needs_reprocess(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        checksums: Sequence[str] | None = None,
    ) -> int: ...

    async def next_dive_for_headtail_prediction(
        self, tenant_id: uuid.UUID, *, predictor_version: int
    ) -> PredictionCandidate | None: ...

    async def headtail_predict_captures(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, predictor_version: int
    ) -> list[PredictCapture]: ...

    async def persist_headtail_predictions(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        rows: Sequence[HeadTailPredictionRow],
    ) -> int: ...


class HeadtailActivities:
    def __init__(
        self, *, catalog: HeadtailCatalog, store: OrchestratorObjectStore
    ) -> None:
        self._catalog = catalog
        self._store = store

    # -- stage 5.1 ------------------------------------------------------------------

    @activity.defn(name="select_next_dive_for_headtail_preprocessing")
    async def select_next_dive_for_headtail_preprocessing(
        self,
    ) -> Optional[HeadtailTarget]:
        """The oldest dive in the stage-5.1 cohort, across tenants."""
        best, best_key = None, None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_headtail_preprocessing(
                tenant_id
            )
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = HeadtailTarget(tenant_id, candidate.dive_id), key
        activity.logger.info("next dive for headtail preprocessing: %s", best)
        return best

    @activity.defn(name="resolve_headtail_preprocess_inputs")
    async def resolve_headtail_preprocess_inputs(
        self, target: HeadtailTarget
    ) -> PreprocessHeadtailImagesInput:
        """The dive's frames to render, where each is staged and where its JPEG
        goes, and the intrinsics to rectify with."""
        inputs = await self._catalog.headtail_preprocess_inputs(
            target.tenant_id, target.dive_id
        )
        images = []
        for capture in inputs.captures:
            images.append(
                PreprocessHeadtailImage(
                    capture_id=capture.capture_id,
                    checksum=capture.checksum,
                    raw=self._store.layout.raw(target.tenant_id, capture.checksum),
                    jpeg=await self._store.processed_jpeg_target(
                        target.tenant_id,
                        HEADTAIL_JPEG_FOLDER,
                        capture.checksum,
                        from_v1=capture.from_v1,
                    ),
                )
            )
        activity.logger.info(
            "resolved headtail preprocess inputs dive=%s images=%d",
            target.dive_id,
            len(images),
        )
        return PreprocessHeadtailImagesInput(
            tenant_id=target.tenant_id,
            dive_id=target.dive_id,
            images=images,
            camera_matrix=inputs.camera_matrix,
            distortion_coefficients=inputs.distortion_coefficients,
        )

    @activity.defn(name="clear_headtail_reprocess_flags")
    async def clear_headtail_reprocess_flags(
        self, request: ClearHeadtailReprocessFlags
    ) -> int:
        """Lower the dive's stage-5.1 redraw flags, scoped to the frames
        redrawn. Idempotent: a dive never flagged clears 0."""
        cleared = await self._catalog.clear_headtail_needs_reprocess(
            request.tenant_id, request.dive_id, request.checksums
        )
        activity.logger.info(
            "cleared headtail reprocess flags dive=%s rows=%d scope=%s",
            request.dive_id,
            cleared,
            "whole dive" if request.checksums is None else len(request.checksums),
        )
        return cleared

    # -- predict --------------------------------------------------------------------

    @activity.defn(name="select_next_dive_for_headtail_prediction")
    async def select_next_dive_for_headtail_prediction(
        self,
    ) -> Optional[HeadtailTarget]:
        """The next dive for the detector across tenants: never-predicted
        work first, so the fallback tier's upgrades can't starve it; then the
        oldest."""
        best, best_key = None, None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_headtail_prediction(
                tenant_id, predictor_version=HEADTAIL_PREDICTOR_VERSION
            )
            if candidate is None:
                continue
            key = (
                not candidate.never_predicted,
                candidate.created_at,
                str(candidate.dive_id),
            )
            if best_key is None or key < best_key:
                best, best_key = HeadtailTarget(tenant_id, candidate.dive_id), key
        activity.logger.info("next dive for headtail prediction: %s", best)
        return best

    @activity.defn(name="resolve_headtail_predict_inputs")
    async def resolve_headtail_predict_inputs(
        self, target: HeadtailTarget
    ) -> PredictHeadtailImagesInput:
        """The dive's images needing a prediction whose stage-5.1 JPEG is
        written; the rest are deferred to a later firing (+30 renders, +32
        predicts, and they overlap routinely)."""
        captures = await self._catalog.headtail_predict_captures(
            target.tenant_id,
            target.dive_id,
            predictor_version=HEADTAIL_PREDICTOR_VERSION,
        )
        images = []
        for capture in captures:
            jpeg = await self._store.locate_processed_jpeg(
                target.tenant_id,
                HEADTAIL_JPEG_FOLDER,
                capture.checksum,
                from_v1=capture.from_v1,
            )
            activity.heartbeat()
            if jpeg is None:
                activity.logger.info(
                    "headtail JPEG not yet written for capture %s; deferring",
                    capture.capture_id,
                )
                continue
            images.append(
                PredictHeadtailImage(
                    capture_id=capture.capture_id,
                    jpeg=jpeg,
                    laser_points=[[d.x, d.y] for d in capture.dots],
                    laser_label_ids=[d.laser_label_id for d in capture.dots],
                    has_existing_prediction=capture.has_existing_prediction,
                    existing_laser_superseded=capture.existing_laser_superseded,
                )
            )
        activity.logger.info(
            "resolved headtail predict inputs dive=%s needing=%d deferred_no_jpeg=%d",
            target.dive_id,
            len(images),
            len(captures) - len(images),
        )
        return PredictHeadtailImagesInput(
            tenant_id=target.tenant_id, dive_id=target.dive_id, images=images
        )

    @activity.defn(name="persist_headtail_predictions")
    async def persist_headtail_predictions(
        self, target: HeadtailTarget, results: List[HeadtailPredictionResult]
    ) -> int:
        """Append each prediction, abstentions included (the cohort selects on
        a row's absence). A refusal is final."""
        refused = sorted({r.status for r in results} - set(HEADTAIL_STATUSES))
        if refused:
            raise ApplicationError(
                f"refusing head/tail results with status {refused} for dive "
                f"{target.dive_id}: not a prediction or an abstention",
                type="InvalidPredictions",
                non_retryable=True,
            )
        rows = [HeadTailPredictionRow(**r.model_dump()) for r in results]
        try:
            written = await self._catalog.persist_headtail_predictions(
                target.tenant_id, target.dive_id, rows
            )
        except InvalidPredictions as exc:
            raise ApplicationError(
                f"refusing the processor's predictions for dive {target.dive_id}: "
                f"{exc}",
                type="InvalidPredictions",
                non_retryable=True,
            ) from exc
        activity.logger.info(
            "persisted %d headtail predictions for dive=%s", written, target.dive_id
        )
        return written
