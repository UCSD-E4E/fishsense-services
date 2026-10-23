"""The local record of an Authentik identity.

v2 stores no credentials. A user row exists so memberships have something to
point at; it is created the first time a valid token for that ``sub`` arrives.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def record_profile(
    conn: AsyncConnection, name: str | None, email: str | None
) -> None:
    """Keep the caller's name and email as their token says (0065): the
    caller's own row only; a missing one leaves what was recorded."""
    await conn.execute(
        text("SELECT record_profile(:name, :email)"), {"name": name, "email": email}
    )


async def provision_user(conn: AsyncConnection, sub: str) -> uuid.UUID:
    """Return the caller's user id, creating the row on first sight."""
    created = (
        await conn.execute(
            text("""
                INSERT INTO users (sub) VALUES (:sub)
                ON CONFLICT (sub) DO NOTHING
                RETURNING id
                """),
            {"sub": sub},
        )
    ).scalar_one_or_none()
    if created is not None:
        return created
    return (
        await conn.execute(text("SELECT id FROM users WHERE sub = :sub"), {"sub": sub})
    ).scalar_one()
