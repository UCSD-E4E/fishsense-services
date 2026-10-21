"""Which captures still need a derived place name, and keeping one (0041).

A capture needs one when it has coordinates and the app sent no name, and
the server has no answer for it yet -- or its last answer was "no named place
in range" long enough ago (``RETRY_NO_NAME_AFTER``) that the geocoder's data
may have changed. An outage is never recorded, so a capture the geocoder
couldn't answer for is simply asked again next time.

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.phone_missions import place_a_phone_mission
from fishsense_services_api.place_names import ReverseGeocode

__all__ = ["RETRY_NO_NAME_AFTER", "Unnamed", "needing_place_names", "record_place_name"]

RETRY_NO_NAME_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class Unnamed:
    capture_id: uuid.UUID
    latitude: float
    longitude: float


async def needing_place_names(
    conn: AsyncConnection, tenant_id: uuid.UUID, capture_ids: Sequence[uuid.UUID]
) -> list[Unnamed]:
    """Those of ``capture_ids`` that need a name derived now."""
    if not capture_ids:
        return []
    rows = await conn.execute(
        text("""
            SELECT c.id, c.latitude, c.longitude
            FROM captures c
            JOIN mobile_captures mc
              ON mc.tenant_id = c.tenant_id AND mc.capture_id = c.id
            LEFT JOIN LATERAL (
                SELECT place_name, derived_at FROM capture_place_names
                WHERE tenant_id = c.tenant_id AND capture_id = c.id
                ORDER BY derived_at DESC LIMIT 1
            ) pn ON true
            WHERE c.tenant_id = :tenant_id AND c.id = ANY(:ids)
              AND c.latitude IS NOT NULL AND c.longitude IS NOT NULL
              AND mc.place_name IS NULL
              AND (pn.derived_at IS NULL
                   OR (pn.place_name IS NULL
                       AND pn.derived_at < now() - CAST(:retry AS interval)))
            """),
        {
            "tenant_id": tenant_id,
            "ids": list(capture_ids),
            "retry": RETRY_NO_NAME_AFTER,
        },
    )
    return [Unnamed(r.id, r.latitude, r.longitude) for r in rows]


async def record_place_name(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    found: ReverseGeocode,
) -> None:
    await conn.execute(
        text("""
            INSERT INTO capture_place_names (tenant_id, capture_id, place_name,
                source, properties)
            VALUES (:tenant_id, :capture_id, :place_name, :source,
                CAST(:properties AS jsonb))
            """),
        {
            "tenant_id": tenant_id,
            "capture_id": capture_id,
            "place_name": found.place_name,
            "source": found.source,
            "properties": json.dumps(found.properties),
        },
    )
    # A phone mission with no place yet takes the one derived for its capture.
    mission_id = (
        await conn.execute(
            text("SELECT mission_id FROM captures WHERE tenant_id = :t AND id = :c"),
            {"t": tenant_id, "c": capture_id},
        )
    ).scalar_one_or_none()
    if mission_id is not None:
        await place_a_phone_mission(conn, tenant_id, mission_id, found.place_name)
