"""Which tenant a request acts in, and with what role.

The tenant named in the URL is honoured only if the caller is a member of it.
The query scopes to the caller explicitly (the app layer's line of defence);
the RLS policies on users, memberships and tenants are the backstop.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

#: The role that may make the edits PLAN.md §4.1 reserves to admins: a dive's
#: calibration source, and Label Studio triage (what v1's portal group,
#: `FishSense-Prod-Admins`, can do today). ``memberships.role`` is free text;
#: only this exact string is an admin, so a near miss fails closed.
ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class Membership:
    tenant_id: uuid.UUID
    role: str
    #: The caller's own user row, so a route needn't look it up again.
    user_id: uuid.UUID | None = field(default=None, compare=False)

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


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
                SELECT m.tenant_id, m.role, u.id AS user_id
                FROM memberships m
                JOIN users u ON u.id = m.user_id
                JOIN tenants t ON t.id = m.tenant_id
                WHERE u.sub = :sub AND t.slug = :slug
                """),
            {"sub": sub, "slug": tenant_slug},
        )
    ).one_or_none()
    if row is None:
        return None
    return Membership(tenant_id=row.tenant_id, role=row.role, user_id=row.user_id)


async def join_claimed_tenant(conn: AsyncConnection) -> None:
    """Make the caller a member of the tenant that claims their org, if any.

    Runs in a :func:`~fishsense_services_api.db.principal_transaction` opened
    with the caller's ``org``; the database function reads both from that
    scope. A no-op without an org, for an org no tenant claims, or for a
    caller already in that tenant -- whatever their role there.
    """
    await conn.execute(text("SELECT join_claimed_tenant()"))


@dataclass(frozen=True)
class TenantMembership:
    """One of the caller's tenants, by name: what ``GET /me/memberships`` lists."""

    slug: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


async def list_memberships(conn: AsyncConnection, sub: str) -> list[TenantMembership]:
    """Every tenant the caller belongs to, by slug -- and no other."""
    rows = await conn.execute(
        text("""
            SELECT t.slug, t.name, m.role
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            JOIN tenants t ON t.id = m.tenant_id
            WHERE u.sub = :sub
            ORDER BY t.slug
            """),
        {"sub": sub},
    )
    return [TenantMembership(**row._mapping) for row in rows]
