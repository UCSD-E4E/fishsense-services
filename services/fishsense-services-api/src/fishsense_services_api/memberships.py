"""Which tenant a request acts in, and with what role.

The tenant named in the URL is honoured only if the caller is a member of it.
The query scopes to the caller explicitly (the app layer's line of defence);
the RLS policies on users, memberships and tenants are the backstop.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class Membership:
    tenant_id: uuid.UUID
    role: str


async def resolve_membership(
    conn: AsyncConnection, sub: str, tenant_slug: str
) -> Membership | None:
    """The caller's membership in ``tenant_slug``, or None if they have none.

    An unknown tenant and a tenant the caller doesn't belong to are
    indistinguishable here, so callers can't probe which tenants exist.
    """
    row = (
        await conn.execute(
            text("""
                SELECT m.tenant_id, m.role
                FROM memberships m
                JOIN users u ON u.id = m.user_id
                JOIN tenants t ON t.id = m.tenant_id
                WHERE u.sub = :sub AND t.slug = :slug
                """),
            {"sub": sub, "slug": tenant_slug},
        )
    ).one_or_none()
    return None if row is None else Membership(tenant_id=row.tenant_id, role=row.role)
