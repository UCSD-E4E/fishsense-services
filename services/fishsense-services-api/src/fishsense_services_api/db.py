"""Database access scoped to one caller or one tenant.

Every query runs inside a scoped transaction. Scope is set with
``set_config(..., is_local => true)`` -- the transaction-local form of
``SET LOCAL`` -- so it ends with the transaction and a pooled connection can
never carry one caller's or tenant's scope into its next checkout.

- :func:`principal_transaction` scopes to the caller (``app.user_sub``): used
  to resolve which tenants they belong to.
- :func:`tenant_transaction` scopes to a tenant (``app.tenant_id``): RLS then
  exposes only that tenant's rows.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@asynccontextmanager
async def principal_transaction(
    bind: AsyncEngine | AsyncConnection, sub: str
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction in which RLS exposes only the caller's own rows."""
    async with _scoped_transaction(bind, {"app.user_sub": sub}) as conn:
        yield conn


@asynccontextmanager
async def tenant_transaction(
    bind: AsyncEngine | AsyncConnection, tenant_id: uuid.UUID
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction in which RLS exposes only ``tenant_id``'s rows."""
    async with _scoped_transaction(bind, {"app.tenant_id": str(tenant_id)}) as conn:
        yield conn


@asynccontextmanager
async def _scoped_transaction(
    bind: AsyncEngine | AsyncConnection, settings: Mapping[str, str]
) -> AsyncIterator[AsyncConnection]:
    if isinstance(bind, AsyncEngine):
        async with bind.begin() as conn:
            await _apply(conn, settings)
            yield conn
    else:
        async with bind.begin():
            await _apply(bind, settings)
            yield bind


async def _apply(conn: AsyncConnection, settings: Mapping[str, str]) -> None:
    for name, value in settings.items():
        await conn.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )
