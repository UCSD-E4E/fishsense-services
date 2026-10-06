"""The slate detector's orchestrator activities: select, resolve, persist.

New in v2. The model is 2026-10-03_slate_detector@95a77d95's presence
classifier (on the processor, `slate_detect`); these activities are built as
laser and species prediction's are, and keep their rules:

* the target is (tenant, dive), and the selector takes the oldest candidate
  across every tenant the orchestrator serves. The cohort is any priority
  (`fishsense_services_api.slate_presence_store`): this is for dives nobody
  labelled;
* **the orchestrator issues the keys** (PLAN.md §9.11): each frame's staged
  raw, which the parent stages before the child runs;
* a dive the resolver cannot resolve (no pinhole camera), and a refusal of
  the processor's output, are final (non-retryable): retrying reads the same
  rows to the same answer.

The predictions feed two readers: stage 9 queues a dive's slate frames for
labelling when it has no slate labels (`slate_store`), and the automatic chain
reads `slate_frames`.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Protocol, Sequence

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.slate_presence_store import (
    InvalidSlatePresence,
    SlateDetectionCandidate,
    SlateDetectionInputs,
    SlateDetectionUnavailable,
    SlatePresenceRow,
)
from fishsense_services_contracts.slate_presence import (
    SLATE_DETECTOR_VERSION,
    DetectSlateImage,
    DetectSlateImagesInput,
    SlatePresenceResult,
    is_slate,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

__all__ = ["SlateDetectionActivities", "SlatePresenceCatalog"]


class SlatePresenceCatalog(Protocol):
    """See ``fishsense_services_api.slate_presence_store.SlatePresenceCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_slate_detection(
        self, tenant_id: uuid.UUID, *, model_version: int
    ) -> SlateDetectionCandidate | None: ...

    async def slate_detection_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, model_version: int
    ) -> SlateDetectionInputs: ...

    async def persist_slate_presence(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        rows: Sequence[SlatePresenceRow],
    ) -> int: ...


class SlateDetectionActivities:
    def __init__(self, *, catalog: SlatePresenceCatalog, layout: ObjectLayout):
        self._catalog = catalog
        self._layout = layout

    @activity.defn(name="select_next_dive_for_slate_detection")
    async def select_next_dive_for_slate_detection(self) -> Optional[StagingTarget]:
        """The oldest dive across tenants with a canonical frame the current
        model has not predicted."""
        best, best_key = None, None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_slate_detection(
                tenant_id, model_version=SLATE_DETECTOR_VERSION
            )
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = StagingTarget(tenant_id, candidate.dive_id), key
        activity.logger.info("next dive for slate detection: %s", best)
        return best

    @activity.defn(name="resolve_slate_detection_inputs")
    async def resolve_slate_detection_inputs(
        self, target: StagingTarget
    ) -> DetectSlateImagesInput:
        """Each frame the current model has not predicted, as its staged raw,
        and the dive's pinhole intrinsics."""
        try:
            inputs = await self._catalog.slate_detection_inputs(
                target.tenant_id,
                target.dive_id,
                model_version=SLATE_DETECTOR_VERSION,
            )
        except SlateDetectionUnavailable as exc:
            raise ApplicationError(
                f"cannot resolve slate detection for dive {target.dive_id}: {exc}",
                type="SlateDetectionUnavailable",
                non_retryable=True,
            ) from exc
        activity.logger.info(
            "resolved slate detection inputs dive=%s images=%d",
            target.dive_id,
            len(inputs.captures),
        )
        return DetectSlateImagesInput(
            tenant_id=target.tenant_id,
            dive_id=target.dive_id,
            camera_matrix=inputs.camera_matrix,
            distortion_coefficients=inputs.distortion_coefficients,
            images=[
                DetectSlateImage(
                    capture_id=capture.capture_id,
                    raw=self._layout.raw(target.tenant_id, capture.checksum),
                )
                for capture in inputs.captures
            ],
        )

    @activity.defn(name="persist_slate_presence_predictions")
    async def persist_slate_presence_predictions(
        self, target: StagingTarget, results: List[SlatePresenceResult]
    ) -> int:
        """Append each prediction as the processor stamped it, abstentions
        included (the cohort selects on a row's absence). A refusal is
        final."""
        rows = [
            SlatePresenceRow(
                capture_id=r.capture_id,
                status=r.status,
                probability=r.probability,
                model_version=r.model_version,
                weights_sha256=r.weights_sha256,
            )
            for r in results
        ]
        if not rows:
            return 0
        try:
            written = await self._catalog.persist_slate_presence(
                target.tenant_id, target.dive_id, rows
            )
        except InvalidSlatePresence as exc:
            raise ApplicationError(
                f"refusing the processor's slate predictions for dive "
                f"{target.dive_id}: {exc}",
                type="InvalidPredictions",
                non_retryable=True,
            ) from exc
        activity.logger.info(
            "persisted %d slate presence predictions dive=%s slate=%d",
            written,
            target.dive_id,
            sum(
                1
                for r in results
                if r.probability is not None and is_slate(r.probability)
            ),
        )
        return written
