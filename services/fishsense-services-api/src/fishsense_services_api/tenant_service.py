"""The orchestrator serves every tenant by default (the owner, 2026-10-21:
"the orchestrator is responsible for running everything").

It works in a tenant only as a member of it (PLAN.md §9.11), so the default is
a membership, never a bypass of row-level security. ``add-tenant`` makes it a
member of the tenant it opens, and ``serve-tenants`` makes it a member of the
tenants opened before. Each grant is a row, which can be read and revoked:
revoking one stops the pipeline for that tenant.

Runs as the schema owner, in the caller's transaction.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["serve", "unserved"]


async def unserved(conn: AsyncConnection, sub: str) -> list[str]:
    """The tenants ``sub`` is not a member of, by slug."""
    rows = await conn.execute(
        text("""
            SELECT t.slug FROM tenants t
            WHERE NOT EXISTS (
                SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id
                WHERE m.tenant_id = t.id AND u.sub = :sub
            )
            ORDER BY t.slug
            """),
        {"sub": sub},
    )
    return list(rows.scalars())


async def serve(conn: AsyncConnection, sub: str, slugs: list[str]) -> int:
    """Make ``sub`` a member of each of ``slugs`` it isn't yet; how many it
    became one of. Its user row is made if this is its first."""
    await conn.execute(
        text("INSERT INTO users (sub) VALUES (:sub) ON CONFLICT (sub) DO NOTHING"),
        {"sub": sub},
    )
    granted = await conn.execute(
        text("""
            INSERT INTO memberships (tenant_id, user_id, role)
            SELECT t.id, u.id, 'member' FROM tenants t, users u
            WHERE t.slug = ANY(:slugs) AND u.sub = :sub
            ON CONFLICT (tenant_id, user_id) DO NOTHING
            """),
        {"sub": sub, "slugs": slugs},
    )
    return granted.rowcount
