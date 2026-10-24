"""Personal API tokens (0066; the owner, 2026-10-23).

Ported in shape from e4e-reference-manager@1cfed39's ``refman_`` tokens
(``api/app/auth.py``):
a random secret shown once, only its SHA-256 kept, an expiry on every token.
v2 adds a kind, fixed when the token is made: ``read`` (the default) or
``edit`` (an opt-in), so a token handed to an agent is refused every write
request unless its owner chose otherwise. Its reads keep their server-side
effects, as anyone's do: a system admin's visit is recorded (0056), and the
Fish list fills in the place names it can.

The table is reached only through 0066's functions, which act for the caller
named by ``app.user_sub``; :func:`token_principal` is the one that needs no
caller, as holding the token is what proves who it is.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: Every token starts with this, which is how the API tells one from a JWT.
TOKEN_PREFIX = "fishsense_"
#: How much of a token is kept to tell tokens apart: the prefix and 6 more.
SHOWN_CHARACTERS = len(TOKEN_PREFIX) + 6
READ, EDIT = "read", "edit"
Scope = Literal["read", "edit"]
DEFAULT_DAYS = 90
#: As 0066 has them (a test pins the two equal): the longest a token lasts,
#: and how many live tokens a user may hold.
MAX_DAYS = 365
MAX_LIVE = 25


@dataclass(frozen=True)
class ApiToken:
    id: uuid.UUID
    name: str
    scope: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class TokenOwner:
    sub: str
    scope: str


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_secret() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


async def create_token(
    conn: AsyncConnection, *, name: str, scope: Scope, days: int
) -> tuple[str, ApiToken] | None:
    """The caller's new token and its secret, or None once they hold
    ``MAX_LIVE`` live tokens: the only quiet refusal. Runs in the caller's
    principal transaction, after their user row is provisioned."""
    raw = new_secret()
    row = (
        await conn.execute(
            text(
                "SELECT * FROM api_token_create(:name, :scope, :hash, :prefix, :days)"
            ),
            {
                "name": name,
                "scope": scope,
                "hash": hash_token(raw),
                "prefix": raw[:SHOWN_CHARACTERS],
                "days": days,
            },
        )
    ).one_or_none()
    return None if row is None else (raw, ApiToken(**row._mapping))


async def my_tokens(conn: AsyncConnection) -> list[ApiToken]:
    rows = await conn.execute(text("SELECT * FROM api_tokens_mine()"))
    return [ApiToken(**row._mapping) for row in rows]


async def revoke_token(conn: AsyncConnection, token_id: uuid.UUID) -> bool:
    """Revoke one of the caller's tokens; False if it isn't theirs."""
    return (
        await conn.execute(text("SELECT api_token_revoke(:id)"), {"id": token_id})
    ).scalar_one()


async def token_principal(engine: AsyncEngine, raw: str) -> TokenOwner | None:
    """Who a live token acts as, and its kind; None if unknown, expired or
    revoked. Records the use (at most once a minute)."""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM api_token_principal(:hash)"),
                {"hash": hash_token(raw)},
            )
        ).one_or_none()
    return None if row is None else TokenOwner(sub=row.sub, scope=row.scope)
