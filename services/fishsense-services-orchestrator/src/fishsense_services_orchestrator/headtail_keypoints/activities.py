"""The SimCC keypoint replay's orchestrator activities (read-only):

- `plan_keypoint_replay`: the tenant's missions with work, and how many frames;
- `resolve_keypoint_page`: a page of one mission's frames, each with the
  render its prediction was made on (at that version and camera: the mask box
  is in its pixels) by presigned GET; one whose render is gone, or a tenant
  with no bucket, is counted and left out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.headtail_keypoints import (
    KeypointFrame,
    KeypointFramesInput,
)

__all__ = ["KeypointPage", "KeypointReplayActivities", "KeypointTarget", "PAGE"]

#: Frames per page (and per GPU child): each carries a presigned GET.
PAGE = 200


@dataclass
class KeypointTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID


@dataclass
class KeypointPage:
    payload: Optional[KeypointFramesInput]
    #: The last frame's seq: the next page starts after it. None: the end.
    last_seq: Optional[int] = None
    #: Frames left out: their render is gone, or the tenant has no bucket.
    unavailable: int = 0


class KeypointReplayActivities:
    def __init__(self, *, catalog: Any, renders: Any) -> None:
        #: `HeadtailKeypointCatalog`.
        self._catalog = catalog
        #: `renders.urls.TenantRenderUrls`.
        self._renders = renders

    @activity.defn(name="plan_keypoint_replay")
    async def plan_keypoint_replay(
        self, tenant: str, numbers: Optional[List[int]] = None
    ) -> dict:
        tenant_id = None
        for t in await self._catalog.member_tenants():
            if await self._catalog.tenant_slug(t) == tenant:
                tenant_id = t
        if tenant_id is None:
            raise ApplicationError(f"no tenant {tenant!r}", type="NoSuchTenant",
                                   non_retryable=True)  # fmt: skip
        missions = await self._catalog.keypoint_replay_missions(tenant_id)
        if numbers:
            missions = [m for m in missions if m.number in set(numbers)]
        return {"tenant_id": str(tenant_id),
                "missions": [{"mission_id": str(m.mission_id), "number": m.number,
                              "frames": m.frames} for m in missions]}  # fmt: skip

    @activity.defn(name="resolve_keypoint_page")
    async def resolve_keypoint_page(
        self, target: KeypointTarget, after_seq: int = 0
    ) -> KeypointPage:
        rows = await self._catalog.keypoint_replay_frames(
            target.tenant_id, target.mission_id, after_seq=after_seq, limit=PAGE
        )
        if not rows:
            return KeypointPage(payload=None)
        if await self._renders.objects(target.tenant_id) is None:
            return KeypointPage(payload=None, last_seq=rows[-1].seq,
                                unavailable=len(rows))  # fmt: skip
        urls = await self._urls(target, rows)
        frames = [KeypointFrame(prediction_id=r.prediction_id, capture_id=r.capture_id,
                                render_url=urls[r.prediction_id], mask_bbox=r.mask_bbox)
                  for r in rows if r.prediction_id in urls]  # fmt: skip
        return KeypointPage(
            payload=KeypointFramesInput(tenant_id=target.tenant_id,
                                        mission_id=target.mission_id, frames=frames),
            last_seq=rows[-1].seq, unavailable=len(rows) - len(frames),
        )  # fmt: skip

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
