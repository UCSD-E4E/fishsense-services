"""A tenant's bucket, looked up once for every stage that reads one.

New in v2 (2026-10-21, review of #219). The renders, thumbnails and raw
staging each asked the catalog for the tenant's slug and the tenant stores for
its bucket, three copies of one rule; a change to how a tenant maps to its
bucket now has one place to go.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from temporalio import activity

from fishsense_services_api.tenant_stores import NoTenantStore

__all__ = ["tenant_objects"]


async def tenant_objects(
    catalog: Any, stores: Any, tenant_id: uuid.UUID, *, waiting: Optional[str] = None
) -> Any:
    """The tenant's bucket (`TenantObjects`), or None when it has none. With
    `waiting`, a tenant with no bucket is logged as what waits for one."""
    slug = await catalog.tenant_slug(tenant_id)
    try:
        return stores.for_tenant(slug)
    except NoTenantStore:
        if waiting:
            activity.logger.warning("tenant %s has no bucket: %s", slug, waiting)
        return None
