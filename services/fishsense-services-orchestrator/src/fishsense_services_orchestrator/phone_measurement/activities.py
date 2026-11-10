"""The phone measurement's orchestrator activities (contract 28).

- `plan_phone_measurement`: the tenant's missions with phone captures owed a
  measurement, and how many (the dry run's plan);
- `resolve_phone_measurement_page`: a page of one mission's owed captures,
  each with presigned GETs for its JPEG and depth (the tenant's bucket, which
  the processor holds no key for), its depth's geometry, its K, and a mask
  upload;
- `persist_phone_measurements`: every result recorded, each stored mask read
  back and checked first (`masks.checked_masks`); a mask that doesn't check
  out is deleted and its result kept without it.

A result is the server's, unvalidated (the owner, 2026-11-09), and goes to
``phone_measurements``, never ``measurements``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.phone_measurement_store import (
    PhoneMeasurementMask,
    PhoneMeasurementResult,
)
from fishsense_services_api.tenant_stores import RENDER_EXPIRES_SECONDS
from fishsense_services_contracts.phone_measurement import (
    PhoneFrame,
    PhoneMeasured,
    PhoneMeasureInput,
)
from fishsense_services_orchestrator.masks import checked_masks, discard, mask_upload
from fishsense_services_orchestrator.object_store.tenant_buckets import tenant_objects

__all__ = [
    "PAGE",
    "PhoneMeasurementActivities",
    "PhoneMeasurementPage",
    "PhoneMeasurementTarget",
]

#: Captures per page (and per processor child): each carries two presigned
#: GETs and a PUT (~650 chars each), well under Temporal's 2 MB.
PAGE = 100
_SOURCE = "phone"


@dataclass
class PhoneMeasurementTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID


@dataclass
class PhoneMeasurementPage:
    payload: Optional[PhoneMeasureInput]
    #: The page's last capture id: the next page starts after it.
    last: Optional[str] = None
    #: Captures left out: the tenant has no bucket.
    unavailable: int = 0
    no_bucket: bool = False


class PhoneMeasurementActivities:
    def __init__(self, *, catalog: Any, stores: Any) -> None:
        #: `PhoneMeasurementCatalog`.
        self._catalog = catalog
        #: `TenantStores`, by slug.
        self._stores = stores

    @activity.defn(name="plan_phone_measurement")
    async def plan_phone_measurement(
        self, tenant: str, numbers: Optional[List[int]] = None
    ) -> dict:
        tenant_id = None
        for t in await self._catalog.member_tenants():
            if await self._catalog.tenant_slug(t) == tenant:
                tenant_id = t
        if tenant_id is None:
            raise ApplicationError(f"no tenant {tenant!r}", type="NoSuchTenant",
                                   non_retryable=True)  # fmt: skip
        missions = await self._catalog.owed_missions(tenant_id)
        if numbers:
            missions = [m for m in missions if m.number in set(numbers)]
        return {
            "tenant": tenant, "tenant_id": str(tenant_id),
            "missions": [{"mission_id": str(m.mission_id), "number": m.number,
                          "captures": m.captures} for m in missions],
        }  # fmt: skip

    @activity.defn(name="resolve_phone_measurement_page")
    async def resolve_phone_measurement_page(
        self, target: PhoneMeasurementTarget, after: Optional[str] = None
    ) -> PhoneMeasurementPage:
        rows = await self._catalog.owed_captures(
            target.tenant_id, target.mission_id,
            after=None if after is None else uuid.UUID(after), limit=PAGE,
        )  # fmt: skip
        if not rows:
            return PhoneMeasurementPage(payload=None)
        last = str(rows[-1].capture_id)
        objects = await tenant_objects(self._catalog, self._stores, target.tenant_id)
        if objects is None:
            return PhoneMeasurementPage(payload=None, last=last,
                                        unavailable=len(rows), no_bucket=True)  # fmt: skip

        def get(key: str) -> str:
            return objects.presign_get(key, expires_seconds=RENDER_EXPIRES_SECONDS)

        frames = [
            PhoneFrame(
                capture_id=r.capture_id, rgb_url=get(r.rgb_key), depth_url=get(r.depth_key),
                depth_width=r.depth_width, depth_height=r.depth_height,
                depth_row_bytes=r.depth_row_bytes, intrinsics=list(r.intrinsics),
                mask=mask_upload(objects, _SOURCE, r.capture_id),
            )  # fmt: skip
            for r in rows
        ]
        return PhoneMeasurementPage(
            payload=PhoneMeasureInput(tenant_id=target.tenant_id, frames=frames),
            last=last,
        )

    @activity.defn(name="persist_phone_measurements")
    async def persist_phone_measurements(
        self, target: PhoneMeasurementTarget, results: List[PhoneMeasured]
    ) -> int:
        objects = await tenant_objects(self._catalog, self._stores, target.tenant_id)
        masks: List[Any] = [None] * len(results)
        if objects is not None:
            # `checked_masks` reads each back: the key the phone source's for
            # its capture, the bytes the ones reported, an 8-bit grayscale PNG.
            views = [SimpleNamespace(capture_id=r.capture_id, mask=r.mask,
                                     width=None if r.mask is None else r.mask.width,
                                     height=None if r.mask is None else r.mask.height)
                     for r in results]  # fmt: skip
            masks, failed = await checked_masks(objects, _SOURCE, views)
            if failed:
                await discard(objects, failed)
        rows = [
            PhoneMeasurementResult(
                capture_id=r.capture_id, status=r.status, length_m=r.length_m,
                snout_x=r.snout_x, snout_y=r.snout_y, fork_x=r.fork_x, fork_y=r.fork_y,
                snout_depth_m=r.snout_depth_m, fork_depth_m=r.fork_depth_m,
                model_id=r.model_id, core_version=r.core_version, algorithm=r.algorithm,
                error=r.error,
                mask=None if m is None else PhoneMeasurementMask(
                    bucket=m.bucket, key=m.key, sha256=m.sha256, size=m.size,
                    width=m.width, height=m.height),
            )  # fmt: skip
            for r, m in zip(results, masks, strict=True)
        ]
        written = await self._catalog.record(target.tenant_id, rows) if rows else 0
        activity.logger.info("phone measurement mission=%s: %d results recorded",
                             target.mission_id, written)  # fmt: skip
        return written
