"""The mask backfill's orchestrator activities.

- `plan_mask_backfill`: the tenant's missions with predictions owed a mask,
  and how many (the dry run's plan);
- `resolve_mask_backfill_page`: a page of one mission's owed predictions, each
  with its image -- the automatic track's render (at the version and camera
  the prediction names) by presigned GET, the head/tail stage's stage-5.1
  JPEG by reference -- and a mask upload; one whose image is gone is counted
  and left out;
- `persist_mask_backfill`: each reproduced mask, read back and checked
  (`masks.checked_masks`), named against its prediction; every other stored
  mask deleted; then every regeneration recorded, stored or not, with what it
  got (0062).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.mask_backfill_store import MaskRegeneration
from fishsense_services_contracts.mask_backfill import (
    BackfilledMask,
    BackfillMaskFrame,
    BackfillMasksInput,
    MaskExpectation,
)
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER
from fishsense_services_orchestrator.masks import checked_masks, discard, mask_upload

__all__ = ["MaskBackfillActivities", "MaskBackfillPage", "MaskBackfillTarget"]

#: Predictions per page (and per GPU child): each carries a presigned GET and
#: a presigned PUT (~650 chars each), well under Temporal's 2 MB.
PAGE = 200


@dataclass
class MaskBackfillTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    source: str


@dataclass
class MaskBackfillPage:
    payload: Optional[BackfillMasksInput]
    #: The last prediction's seq: the next page starts after it.
    last_seq: Optional[int] = None
    #: Predictions left out: their image is gone, or the tenant has no bucket.
    unavailable: int = 0
    no_bucket: bool = False


class MaskBackfillActivities:
    def __init__(self, *, catalog: Any, renders: Any, store: Any) -> None:
        #: `MaskBackfillCatalog`.
        self._catalog = catalog
        #: `renders.urls.TenantRenderUrls`: renders, their GETs, the bucket.
        self._renders = renders
        #: `OrchestratorObjectStore`: where a stage-5.1 JPEG is.
        self._store = store

    @activity.defn(name="plan_mask_backfill")
    async def plan_mask_backfill(self, tenant: str, source: str,
                                 numbers: Optional[List[int]] = None) -> dict:  # fmt: skip
        tenant_id = None
        for t in await self._catalog.member_tenants():
            if await self._catalog.tenant_slug(t) == tenant:
                tenant_id = t
        if tenant_id is None:
            raise ApplicationError(f"no tenant {tenant!r}", type="NoSuchTenant",
                                   non_retryable=True)  # fmt: skip
        missions = await self._catalog.unmasked_missions(tenant_id, source)
        if numbers:
            missions = [m for m in missions if m.number in set(numbers)]
        return {
            "tenant": tenant, "tenant_id": str(tenant_id), "source": source,
            "missions": [{"mission_id": str(m.mission_id), "number": m.number,
                          "predictions": m.predictions} for m in missions],
        }  # fmt: skip

    @activity.defn(name="resolve_mask_backfill_page")
    async def resolve_mask_backfill_page(
        self, target: MaskBackfillTarget, after_seq: int = 0
    ) -> MaskBackfillPage:
        rows = await self._catalog.unmasked_predictions(
            target.tenant_id, target.source, target.mission_id,
            after_seq=after_seq, limit=PAGE,
        )  # fmt: skip
        if not rows:
            return MaskBackfillPage(payload=None)
        objects = await self._renders.objects(target.tenant_id)
        if objects is None:
            return MaskBackfillPage(payload=None, last_seq=rows[-1].seq,
                                    unavailable=len(rows), no_bucket=True)  # fmt: skip
        images = await self._images(target, rows)
        frames = []
        for r in rows:
            image = images.get(r.prediction_id)
            if image is None:
                continue
            frames.append(BackfillMaskFrame(
                prediction_id=r.prediction_id, capture_id=r.capture_id,
                dots=[list(r.dot)],
                laser_label_ids=None if r.laser_label_id is None else [r.laser_label_id],
                expect=MaskExpectation(mask_area_px=r.mask_area_px, crop_x=r.crop_x,
                                       crop_y=r.crop_y, head_x=r.head_x, head_y=r.head_y,
                                       tail_x=r.tail_x, tail_y=r.tail_y),
                mask=mask_upload(objects, target.source, r.capture_id),
                **image,
            ))  # fmt: skip
        return MaskBackfillPage(
            payload=BackfillMasksInput(tenant_id=target.tenant_id,
                                       source=target.source, frames=frames),
            last_seq=rows[-1].seq, unavailable=len(rows) - len(frames),
        )  # fmt: skip

    async def _images(self, target, rows) -> Dict[uuid.UUID, dict]:
        """Each prediction's image, as a frame field: ``render_url`` or ``jpeg``."""
        out: Dict[uuid.UUID, dict] = {}
        if target.source == "automatic":
            presign = await self._renders.presigner(target.tenant_id)
            groups: Dict[tuple, list] = {}
            for r in rows:
                groups.setdefault(
                    (r.render_version, r.camera_calibration_id), []
                ).append(r)
            for (version, camera), rs in groups.items():
                renders = await self._renders.at(
                    target.tenant_id, [r.capture_id for r in rs],
                    render_version=version, camera_calibration_id=camera,
                )  # fmt: skip
                for r in rs:
                    render = renders.get(r.capture_id)
                    if (
                        render is not None
                        and render.status == "rendered"
                        and render.key
                    ):
                        out[r.prediction_id] = {"render_url": presign(render.key)}
        else:
            for r in rows:
                jpeg = await self._store.locate_processed_jpeg(
                    target.tenant_id,
                    HEADTAIL_JPEG_FOLDER,
                    r.checksum,
                    from_v1=r.from_v1,
                )
                if jpeg is not None:
                    out[r.prediction_id] = {"jpeg": jpeg}
                activity.heartbeat()
        return out

    @activity.defn(name="persist_mask_backfill")
    async def persist_mask_backfill(
        self, target: MaskBackfillTarget, results: List[BackfilledMask]
    ) -> int:
        objects = await self._renders.objects(target.tenant_id)
        stored = [r for r in results if r.mask is not None]
        written = 0
        if objects is not None and stored:
            # `checked_masks` reads each back: the key the source's for its
            # capture, the bytes the ones reported, an 8-bit greyscale PNG.
            views = [SimpleNamespace(capture_id=r.capture_id, mask=r.mask,
                                     width=r.mask.width, height=r.mask.height)
                     for r in stored]  # fmt: skip
            masks, failed = await checked_masks(objects, target.source, views)
            good = [
                (r.prediction_id, m) for r, m in zip(stored, masks) if m is not None
            ]
            if good:
                written = await self._catalog.record_backfilled_masks(
                    target.tenant_id, target.source, [p for p, _ in good],
                    [m for _, m in good],
                )  # fmt: skip
            if failed:
                await discard(objects, failed)
        # Every regeneration, stored or not, after its mask is named (0062):
        # what the keypointer got on the regenerated mask, for its repeatability.
        if results:
            await self._catalog.record_mask_regenerations(
                target.tenant_id, target.source,
                [MaskRegeneration(prediction_id=r.prediction_id, capture_id=r.capture_id,
                                  outcome=r.outcome,
                                  **({} if r.got is None else r.got.model_dump()))
                 for r in results],
            )  # fmt: skip
        activity.logger.info("mask backfill %s mission=%s: %d masks named",
                             target.source, target.mission_id, written)  # fmt: skip
        return written
