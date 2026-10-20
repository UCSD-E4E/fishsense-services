"""The thumbnail stage's orchestrator activities.

New in v2 (2026-10-20; review of #151: /fish's cards were full renders, ~6-8
MB each). Every render in a tenant's current bucket is owed a thumbnail
(`contracts.thumbnails`), made from the stored render -- never the NAS. The
orchestrator presigns a GET of each render and a PUT of its thumbnail for the
processor, which holds no tenant credentials, and records a thumbnail only
once the stored object matches the size and MD5 the processor reported. A
render not there to read is recorded too, so it is not tried every hour.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.tenant_stores import RENDER_EXPIRES_SECONDS, NoTenantStore
from fishsense_services_api.thumbnails_store import ThumbnailRow
from fishsense_services_contracts.thumbnails import (
    THUMBNAIL_WIDTH,
    ThumbnailFrame,
    ThumbnailResult,
    ThumbnailsInput,
    thumbnail_key,
)

__all__ = [
    "THUMBNAIL_PAGE",
    "ThumbnailActivities",
    "ThumbnailPage",
    "ThumbnailPersistReport",
]

#: Renders per page: each frame carries two presigned URLs (~1.2 kB), and a
#: page rides as an activity result and a child's input (2 MB payload limit).
THUMBNAIL_PAGE = 500


@dataclass
class ThumbnailPage:
    """A page of a tenant's owed thumbnails; None when nothing is owed or the
    tenant has no bucket."""

    payload: Optional[ThumbnailsInput]


@dataclass
class ThumbnailPersistReport:
    made: int = 0
    #: Renders not there to read.
    missing: int = 0
    #: Made, but the stored object did not match what was reported.
    unverified: List[uuid.UUID] = field(default_factory=list)


def _refuse(tenant_id: uuid.UUID, why: str) -> ApplicationError:
    return ApplicationError(
        f"refusing the processor's thumbnails for tenant {tenant_id}: {why}",
        type="InvalidThumbnails",
        non_retryable=True,
    )


class ThumbnailActivities:
    def __init__(self, *, catalog: Any, stores: Any) -> None:
        self._catalog = catalog
        #: The tenant stores (`TenantStores`), by slug.
        self._stores = stores

    async def _objects(self, tenant_id: uuid.UUID):
        slug = await self._catalog.tenant_slug(tenant_id)
        try:
            return self._stores.for_tenant(slug)
        except NoTenantStore:
            activity.logger.warning(
                "tenant %s has no bucket: its renders wait for their thumbnails", slug
            )
            return None

    @activity.defn(name="select_thumbnail_tenants")
    async def select_thumbnail_tenants(self) -> List[uuid.UUID]:
        """Every tenant served that has a bucket."""
        return [
            t
            for t in await self._catalog.member_tenants()
            if await self._objects(t) is not None
        ]

    @activity.defn(name="resolve_thumbnails")
    async def resolve_thumbnails(
        self, tenant_id: uuid.UUID, limit: int = THUMBNAIL_PAGE
    ) -> ThumbnailPage:
        """A page of the tenant's renders owing a thumbnail, each with a GET of
        the render and a PUT of its thumbnail."""
        objects = await self._objects(tenant_id)
        if objects is None:
            return ThumbnailPage(payload=None)
        owed = await self._catalog.owed_thumbnails(
            tenant_id, bucket=objects.bucket, width=THUMBNAIL_WIDTH, limit=limit
        )
        if not owed:
            return ThumbnailPage(payload=None)
        frames = []
        for o in owed:
            key = thumbnail_key(o.render_key)
            frames.append(
                ThumbnailFrame(
                    render_id=o.render_id,
                    capture_id=o.capture_id,
                    get_url=objects.presign_get(
                        o.render_key, expires_seconds=RENDER_EXPIRES_SECONDS
                    ),
                    key=key,
                    put_url=objects.presign_render_put(key).url,
                )
            )
            activity.heartbeat()
        return ThumbnailPage(
            payload=ThumbnailsInput(
                tenant_id=tenant_id, width=THUMBNAIL_WIDTH, frames=frames
            )
        )

    @activity.defn(name="persist_thumbnails")
    async def persist_thumbnails(
        self,
        tenant_id: uuid.UUID,
        payload: ThumbnailsInput,
        results: List[ThumbnailResult],
    ) -> ThumbnailPersistReport:
        """Record the thumbnails the stored objects confirm, and the renders
        that were not there."""
        objects = await self._objects(tenant_id)
        if objects is None:
            raise _refuse(tenant_id, "the tenant has no bucket")
        asked = {f.render_id: f for f in payload.frames}
        report = ThumbnailPersistReport()
        rows: List[ThumbnailRow] = []
        for r in results:
            frame = asked.get(r.render_id)
            if frame is None:
                raise _refuse(tenant_id, f"render {r.render_id} was not asked for")
            if r.status != "made":
                report.missing += 1
                rows.append(ThumbnailRow(render_id=r.render_id,
                                         capture_id=frame.capture_id,
                                         status=r.status,
                                         target_width=payload.width))  # fmt: skip
                continue
            stored = await asyncio.to_thread(objects.head, frame.key)
            if stored is None or stored.size != r.bytes or stored.md5 != r.md5:
                activity.logger.warning(
                    "tenant=%s render=%s: stored thumbnail %s does not match the "
                    "processor's (%s bytes, md5 %s); left owed",
                    tenant_id, r.render_id, stored, r.bytes, r.md5,
                )  # fmt: skip
                report.unverified.append(r.render_id)
                continue
            report.made += 1
            rows.append(
                ThumbnailRow(
                    render_id=r.render_id, capture_id=frame.capture_id,
                    status="made", target_width=payload.width,
                    bucket=objects.bucket, key=frame.key, bytes=r.bytes, md5=r.md5,
                    width=r.width, height=r.height,
                )  # fmt: skip
            )
        await self._catalog.persist_thumbnails(tenant_id, rows)
        activity.logger.info(
            "tenant=%s thumbnails made=%d missing=%d unverified=%d",
            tenant_id, report.made, report.missing, len(report.unverified),
        )  # fmt: skip
        return report
