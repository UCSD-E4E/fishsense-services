"""Presigned reads of captures' renders, for the steps that read them.

The render is in the tenant's own bucket, and only the API and the
orchestrator hold its key: a step on the processor (SAM, species, the
label-free calibration) is given a presigned GET. Valid a week, as a payload
can wait in a queue for hours. A tenant with no bucket has no renders; asking
for one is a refusal, not a retry.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio.exceptions import ApplicationError

from fishsense_services_api.renders_store import CurrentRender
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

    async def current(
        self, tenant_id: uuid.UUID, capture_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, CurrentRender]:
        """Each capture's newest recorded render."""
        return await self._catalog.current_renders(tenant_id, list(capture_ids))

    async def get_url(self, tenant_id: uuid.UUID, key: str) -> str:
        """A presigned GET of `key` in the tenant's bucket."""
        slug = await self._catalog.tenant_slug(tenant_id)
        try:
            objects = self._stores.for_tenant(slug)
        except NoTenantStore as exc:
            raise ApplicationError(
                f"tenant {slug} has no bucket, so no renders",
                type="TenantHasNoBucket",
                non_retryable=True,
            ) from exc
        return objects.presign_get(key, expires_seconds=RENDER_EXPIRES_SECONDS)
