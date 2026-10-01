"""What the post-converge smoke test asks the database (PLAN.md §6.6 step 6).

The smoke test runs in the orchestrator's image, but only this package touches
the database (repo-root tests/test_database_ownership.py), so its queries live
here and it only opens the connections they run on. Read-only, all of them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["lab_tenant_id", "migration_revision", "research_measurement_count"]


async def migration_revision(conn: AsyncConnection) -> str | None:
    """The schema's alembic revision; None on a database never migrated."""
    return (
        await conn.execute(
            text(
                "SELECT version_num FROM alembic_version "
                "WHERE to_regclass('alembic_version') IS NOT NULL"
            )
        )
    ).scalar_one_or_none()


async def lab_tenant_id(conn: AsyncConnection) -> uuid.UUID | None:
    """The lab tenant's id. Ask as the owner, which bypasses RLS (the
    bootstrap makes it BYPASSRLS, as migrate-v1 requires), so `tenants` shows
    it every row."""
    return (
        await conn.execute(text("SELECT id FROM tenants WHERE slug = 'lab'"))
    ).scalar_one_or_none()


async def research_measurement_count(conn: AsyncConnection, dive_number: int) -> int:
    """A dive's measured lengths through the `v1` research views (0031), by
    v1's dive id, the way imwut's and cscw's extracts join them. Ask as a
    research login."""
    return (
        await conn.execute(
            text(
                "SELECT count(*) FROM v1.measurement m "
                "JOIN v1.image i ON i.id = m.image_id "
                "WHERE i.dive_id = :dive AND m.length_m IS NOT NULL"
            ),
            {"dive": dive_number},
        )
    ).scalar_one()
