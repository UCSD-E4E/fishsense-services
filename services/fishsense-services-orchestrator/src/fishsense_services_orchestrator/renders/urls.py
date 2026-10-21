"""Presigned reads of captures' renders, for the steps that read them.

The render is in the tenant's own bucket, and only the API and the
orchestrator hold its key: a step on the processor (SAM, species, the
label-free calibration) is given a presigned GET. Valid a week, as a payload
can wait in a queue for hours. A tenant with no bucket has no renders; asking
for one is a refusal, not a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fishsense_services_orchestrator.object_store.tenant_buckets import tenant_objects
from temporalio.exceptions import ApplicationError

from fishsense_services_api.renders_store import RenderAt
from fishsense_services_api.tenant_stores import (
    RENDER_EXPIRES_SECONDS,
    NoTenantStore,
)

__all__ = ["TenantRenderUrls"]


class TenantRenderUrls:
    def __init__(self, *, catalog: Any, stores: Any) -> None:
        #: `RendersCatalog`: the renders recorded, and the tenant's slug.
        self._catalog = catalog
        #: `TenantStores`, by slug.
        self._stores = stores

    async def at(
        self,
        tenant_id: uuid.UUID,
        capture_ids: list[uuid.UUID],
        *,
        render_version: int,
        camera_calibration_id: uuid.UUID,
    ) -> dict[uuid.UUID, RenderAt]:
        """Each capture's render at this version under this camera."""
        return await self._catalog.renders_at(
            tenant_id,
            list(capture_ids),
            render_version=render_version,
            camera_calibration_id=camera_calibration_id,
        )

    async def objects(self, tenant_id: uuid.UUID) -> Any:
        """The tenant's bucket (`TenantObjects`), or None when it has none:
        where stored masks are presigned and read back (contract 12)."""
        return await tenant_objects(self._catalog, self._stores, tenant_id)

    async def presigner(self, tenant_id: uuid.UUID) -> Callable[[str], str]:
        """Presigned GETs in the tenant's bucket: the slug looked up once, the
        signing local (review of #144: once per frame was a database round
        trip per frame)."""
        slug = await self._catalog.tenant_slug(tenant_id)
        try:
            objects = self._stores.for_tenant(slug)
        except NoTenantStore as exc:
            raise ApplicationError(
                f"tenant {slug} has no bucket, so no renders",
                type="TenantHasNoBucket",
                non_retryable=True,
            ) from exc
        return lambda key: objects.presign_get(
            key, expires_seconds=RENDER_EXPIRES_SECONDS
        )
