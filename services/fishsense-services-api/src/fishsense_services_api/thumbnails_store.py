"""Each render's thumbnail (0053): which renders are owed one, and what was
made, tenant-scoped.

New in v2 (2026-10-20; review of #151: /fish's cards were full renders). A
render with status ``rendered`` in the tenant's current bucket is owed a
thumbnail at a target width until a row records one made, or the render
missing when read. The thumbnail is made from the stored render, never the
NAS, so the backlog is cheap; the orchestrator checks the stored object before
the row is written (`contracts.thumbnails`).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "THUMBNAIL_MAX_FAILURES",
    "THUMBNAIL_WIDTH",
    "InvalidThumbnails",
    "OwedThumbnail",
    "ThumbnailRow",
    "ThumbnailsCatalog",
    "owed_thumbnails",
    "persist_thumbnails",
    "thumbnail_key",
]

#: The cards' width and the thumbnail's key: `contracts.thumbnails`, repeated
#: because the API image does not install the contracts;
#: tests/test_thumbnails_store.py pins them together.
THUMBNAIL_WIDTH = 640
#: Failed tries (left out after the processor's own, or stored wrong) before
#: a render is no longer offered (review of the stack).
THUMBNAIL_MAX_FAILURES = 3


def thumbnail_key(render_key: str, width: int = THUMBNAIL_WIDTH) -> str:
    return f"thumbnails/w{width}/{render_key}"


class InvalidThumbnails(ValueError):
    """A thumbnail row names a render that is not the tenant's, or not of its
    capture."""


@dataclass(frozen=True)
class OwedThumbnail:
    render_id: uuid.UUID
    capture_id: uuid.UUID
    #: The render's key, in the tenant's current bucket: what is read.
    render_key: str


@dataclass(frozen=True)
class ThumbnailRow:
    """One render's thumbnail, or that the render was not there, to append."""

    render_id: uuid.UUID
    capture_id: uuid.UUID
    status: str
    target_width: int
    bucket: str | None = None
    key: str | None = None
    bytes: int | None = None
    md5: str | None = None
    width: int | None = None
    height: int | None = None


async def owed_thumbnails(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    bucket: str,
    width: int,
    limit: int | None = None,
) -> list[OwedThumbnail]:
    """The tenant's renders in `bucket` with no settled thumbnail row at
    `width` and fewer than `THUMBNAIL_MAX_FAILURES` failed tries: those never
    tried first, then the fewest failures, then the oldest."""
    rows = await conn.execute(
        text("""
            SELECT r.id, r.capture_id, r.key, tries.failed
            FROM capture_renders r
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE h.status = 'failed') AS failed,
                       bool_or(h.status <> 'failed') AS settled
                FROM capture_thumbnails h
                WHERE h.tenant_id = r.tenant_id AND h.render_id = r.id
                  AND h.target_width = :w
            ) tries
            WHERE r.tenant_id = :t AND r.status = 'rendered' AND r.bucket = :bucket
              AND NOT coalesce(tries.settled, false)
              AND tries.failed < :max_failures
            ORDER BY tries.failed, r.seq
            LIMIT :limit
            """),
        {"t": tenant_id, "bucket": bucket, "w": width, "limit": limit,
         "max_failures": THUMBNAIL_MAX_FAILURES},
    )  # fmt: skip
    return [OwedThumbnail(r.id, r.capture_id, r.key) for r in rows]


async def persist_thumbnails(
    conn: AsyncConnection, tenant_id: uuid.UUID, rows: list[ThumbnailRow]
) -> int:
    """Append thumbnails, all or nothing; one already recorded for its render
    and width is not recorded twice. Returns how many rows were written."""
    rows = list(rows)
    if not rows:
        return 0
    found = {
        r.id: r.capture_id
        for r in await conn.execute(
            text(
                "SELECT id, capture_id FROM capture_renders "
                "WHERE tenant_id = :t AND id = ANY(:ids)"
            ),
            {"t": tenant_id, "ids": [r.render_id for r in rows]},
        )
    }
    for r in rows:
        if found.get(r.render_id) != r.capture_id:
            raise InvalidThumbnails(
                f"render {r.render_id} is not capture {r.capture_id}'s"
            )
    written = 0
    for r in rows:
        values = asdict(r)
        columns = ", ".join(["tenant_id", *values])
        params = ", ".join([":tenant_id", *(f":{k}" for k in values)])
        result = await conn.execute(
            text(f"""
                INSERT INTO capture_thumbnails ({columns}) VALUES ({params})
                ON CONFLICT (tenant_id, render_id, target_width)
                    WHERE status IN ('made', 'render_unavailable') DO NOTHING
                """),
            {"tenant_id": tenant_id, **values},
        )
        written += result.rowcount
    return written


class ThumbnailsCatalog(ServicePrincipal):
    """The thumbnail stage's database side, as the orchestrator's service
    principal: every call rechecks its membership of the tenant."""

    async def owed_thumbnails(
        self, tenant_id: uuid.UUID, **kwargs
    ) -> list[OwedThumbnail]:
        async with self._tenant(tenant_id) as conn:
            return await owed_thumbnails(conn, tenant_id, **kwargs)

    async def persist_thumbnails(
        self, tenant_id: uuid.UUID, rows: list[ThumbnailRow]
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_thumbnails(conn, tenant_id, rows)
