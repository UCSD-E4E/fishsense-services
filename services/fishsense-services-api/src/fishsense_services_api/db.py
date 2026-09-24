"""Database access scoped to one tenant.

Every tenant-scoped query runs inside :func:`tenant_transaction`. The tenant is
set with ``set_config(..., is_local => true)`` -- the transaction-local form of
``SET LOCAL`` -- so it ends with the transaction and a pooled connection can
never carry one tenant into its next checkout.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@asynccontextmanager
async def tenant_transaction(
    bind: AsyncEngine | AsyncConnection, tenant_id: uuid.UUID
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction in which RLS exposes only ``tenant_id``'s rows."""
    if isinstance(bind, AsyncEngine):
        async with bind.begin() as conn:
            await _activate(conn, tenant_id)
            yield conn
    else:
        async with bind.begin():
            await _activate(bind, tenant_id)
            yield bind


async def _activate(conn: AsyncConnection, tenant_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
