"""A service acting in tenants as a member of them, never with a bypass.

The orchestrator acts for a tenant only as a member of it (PLAN.md §9.11):
tenants it serves are the ones it holds a membership in, granted by an admin
and auditable like anyone's. Every tenant-keyed call **re-checks** that
membership before opening the tenant's transaction, so a tenant id obtained
earlier is not a standing licence -- work whose membership is revoked
mid-flight stops at its next call.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fishsense_services_api.db import principal_transaction, tenant_transaction
from fishsense_services_api.memberships import resolve_membership

__all__ = ["NotAMember", "ServicePrincipal"]


class NotAMember(PermissionError):
    """The service is not (or is no longer) a member of the tenant."""


class ServicePrincipal:
    def __init__(self, engine: AsyncEngine, *, sub: str) -> None:
        self._engine = engine
        self._sub = sub

    async def resolve_tenant(self, slug: str) -> uuid.UUID | None:
        """The tenant's id, if this service is a member of it -- an unknown
        tenant and an unserved one look the same."""
        async with principal_transaction(self._engine, self._sub) as conn:
            membership = await resolve_membership(conn, self._sub, slug)
        return None if membership is None else membership.tenant_id

    async def member_tenants(self) -> list[uuid.UUID]:
        """Every tenant this service is a member of."""
        async with principal_transaction(self._engine, self._sub) as conn:
            rows = await conn.execute(
                text("""
                    SELECT m.tenant_id FROM memberships m
                    JOIN users u ON u.id = m.user_id
                    WHERE u.sub = :sub ORDER BY m.tenant_id
                    """),
                {"sub": self._sub},
            )
            return list(rows.scalars())

    @asynccontextmanager
    async def _tenant(self, tenant_id: uuid.UUID) -> AsyncIterator[AsyncConnection]:
        async with principal_transaction(self._engine, self._sub) as conn:
            member = (
                await conn.execute(
                    text("""
                        SELECT EXISTS (
                            SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id
                            WHERE u.sub = :sub AND m.tenant_id = :tenant
                        )
                        """),
                    {"sub": self._sub, "tenant": tenant_id},
                )
            ).scalar_one()
        if not member:
            raise NotAMember(f"{self._sub} is not a member of tenant {tenant_id}")
        async with tenant_transaction(self._engine, tenant_id) as conn:
            yield conn
