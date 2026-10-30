"""The laser-color backfill's orchestrator activities (2026-10-26):

- `plan_colour_backfill`: the tenant's missions with automatic dots, and how
  many;
- `resolve_colour_page`: a page of one mission's dots, each with the render
  its prediction was made on (at that version and camera: the dot is in its
  pixels) by presigned GET; one whose render is gone, or a tenant with no
  bucket, is counted and left out (the keypoint replay's rule);
- `record_colour_page`: a page's readings counted and compared with the
  labelers' and the live kernel's colors, and written only with ``apply``
  (`laser_colour_backfill_store.record_colour_page`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.laser_colour_backfill_store import ColourReadingRow
from fishsense_services_contracts.laser_colour_backfill import (
    ColourFrame,
    ColourFramesInput,
    ColourReading,
)

__all__ = ["ColourBackfillActivities", "ColourPage", "ColourTarget", "PAGE"]

#: Dots per page (and per light child): each carries a presigned GET.
PAGE = 200


@dataclass
class ColourTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID


@dataclass
class ColourPage:
    payload: Optional[ColourFramesInput]
    #: The last dot's seq: the next page starts after it. None: the end.
    last_seq: Optional[int] = None
    #: Dots left out: their render is gone, or the tenant has no bucket.
    unavailable: int = 0


class ColourBackfillActivities:
    def __init__(self, *, catalog: Any, renders: Any) -> None:
        #: `ColourBackfillCatalog`.
        self._catalog = catalog
        #: `renders.urls.TenantRenderUrls`.
        self._renders = renders

    @activity.defn(name="plan_colour_backfill")
    async def plan_colour_backfill(
        self, tenant: str, numbers: Optional[List[int]] = None
    ) -> dict:
        tenant_id = None
        for t in await self._catalog.member_tenants():
            if await self._catalog.tenant_slug(t) == tenant:
                tenant_id = t
        if tenant_id is None:
            raise ApplicationError(f"no tenant {tenant!r}", type="NoSuchTenant",
                                   non_retryable=True)  # fmt: skip
        missions = await self._catalog.colour_backfill_missions(tenant_id)
        if numbers:
            missions = [m for m in missions if m.number in set(numbers)]
        return {"tenant_id": str(tenant_id),
                "missions": [{"mission_id": str(m.mission_id), "number": m.number,
                              "frames": m.frames} for m in missions]}  # fmt: skip

    @activity.defn(name="resolve_colour_page")
    async def resolve_colour_page(
        self, target: ColourTarget, after_seq: int = 0
    ) -> ColourPage:
        rows = await self._catalog.colour_backfill_frames(
            target.tenant_id, target.mission_id, after_seq=after_seq, limit=PAGE
        )
        if not rows:
            return ColourPage(payload=None)
        if await self._renders.objects(target.tenant_id) is None:
            return ColourPage(payload=None, last_seq=rows[-1].seq,
                              unavailable=len(rows))  # fmt: skip
        urls = await self._urls(target, rows)
        frames = [ColourFrame(prediction_id=r.prediction_id, capture_id=r.capture_id,
                              render_url=urls[r.prediction_id], x=r.x, y=r.y)
                  for r in rows if r.prediction_id in urls]  # fmt: skip
        return ColourPage(
            payload=ColourFramesInput(tenant_id=target.tenant_id,
                                      mission_id=target.mission_id, frames=frames),
            last_seq=rows[-1].seq, unavailable=len(rows) - len(frames),
        )  # fmt: skip

    @activity.defn(name="record_colour_page")
    async def record_colour_page(
        self, target: ColourTarget, readings: List[ColourReading], apply: bool
    ) -> dict:
        rows = [ColourReadingRow(prediction_id=r.prediction_id, capture_id=r.capture_id,
                                 status=r.status, color=r.color, margin=r.margin)
                for r in readings]  # fmt: skip
        return await self._catalog.record_colour_page(
            target.tenant_id, target.mission_id, rows, apply=apply
        )

    async def _urls(self, target, rows) -> Dict[uuid.UUID, str]:
        presign = await self._renders.presigner(target.tenant_id)
        groups: Dict[tuple, list] = {}
        for r in rows:
            groups.setdefault((r.render_version, r.camera_calibration_id), []).append(r)
        out: Dict[uuid.UUID, str] = {}
        for (version, camera), rs in groups.items():
            renders = await self._renders.at(
                target.tenant_id, [r.capture_id for r in rs],
                render_version=version, camera_calibration_id=camera,
            )  # fmt: skip
            for r in rs:
                render = renders.get(r.capture_id)
                if render is not None and render.status == "rendered" and render.key:
                    out[r.prediction_id] = presign(render.key)
        return out
