"""Phone captures grouped into missions as they sync (Deep water phase 7; the
owner, 2026-10-21).

A Lite mission is a folder of photos off one camera. A phone has no folders,
so its missions come from when and where its captures were taken. A capture
joins the mission of the same phone's capture nearest in time, if that one is
at most :data:`GAP` away and at most :data:`REACH_M` off. Otherwise it starts
a mission of its own. A capture without a location can't be shown to be more
than 1 km off, so time alone decides for it.

Captures sync in any order. A late one from before a mission's first capture
joins it and moves its start earlier. One that falls between two missions
joins the nearer and merges nothing, because missions keep their numbers once
given.

A phone mission is named like a Lite deployment, ``YYYY.MM.DD.FishSense.<place>``.
The day is the phone's local day, from the IANA ``time_zone`` the app sends;
when it sends none, the day is UTC's. The place is the one the phone named,
else ``Mobile``.

The mission's place (``site``) is the first one known for any of its captures:
the phone's own name, or the one the server derives later from the coordinates
(0041, :func:`place_a_phone_mission`). A mission still named ``…FishSense.Mobile``
takes that place into its name. A place once known is kept (phase 7b).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

#: The owner's thresholds (2026-10-21): more than this apart is another mission.
GAP = timedelta(hours=2)
REACH_M = 1000.0

#: Great-circle metres between a capture ``c`` and the point (:lat, :lon).
_METRES = """
    2 * 6371000 * asin(sqrt(
        power(sin(radians(c.latitude - CAST(:lat AS double precision)) / 2), 2)
        + cos(radians(CAST(:lat AS double precision))) * cos(radians(c.latitude))
          * power(sin(radians(c.longitude - CAST(:lon AS double precision)) / 2), 2)
    ))
"""


def local_zone(name: object) -> ZoneInfo | type[UTC]:
    """The phone's zone, or UTC for none or one this server doesn't know."""
    if not isinstance(name, str) or not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def mission_name(captured_at: datetime, time_zone: object, place: str | None) -> str:
    day = captured_at.astimezone(local_zone(time_zone)).date()
    return f"{day:%Y.%m.%d}.FishSense.{(place or '').strip() or 'Mobile'}"


async def place_a_phone_mission(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    place: str | None,
) -> None:
    """Give a phone mission with no place yet ``place``, in its name too."""
    place = (place or "").strip()
    if not place:
        return
    await conn.execute(
        text("""
            UPDATE missions
            SET site = :place,
                name = regexp_replace(name, '[.]FishSense[.]Mobile$', '.FishSense.' || :place)
            WHERE tenant_id = :t AND id = :m AND source = 'mobile' AND site IS NULL
            """),
        {"t": tenant_id, "m": mission_id, "place": place},
    )


async def assign_phone_mission(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    capture_id: uuid.UUID,
    device_id: uuid.UUID,
    captured_at: datetime,
    latitude: float | None,
    longitude: float | None,
    place: str | None,
    time_zone: object,
) -> uuid.UUID:
    """Put a newly committed phone capture in its mission; return the mission."""
    # One phone's captures are grouped one at a time, so two syncing at once
    # can't each start a mission for the same outing.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"phone-missions:{tenant_id}:{device_id}"},
    )
    params = {
        "t": tenant_id,
        "d": device_id,
        "me": capture_id,
        "at": captured_at,
        "earliest": captured_at - GAP,
        "latest": captured_at + GAP,
        "lat": latitude,
        "lon": longitude,
        "reach": REACH_M,
    }
    mission_id = (
        await conn.execute(
            text(f"""
                SELECT c.mission_id
                FROM captures c
                JOIN missions m ON m.tenant_id = c.tenant_id AND m.id = c.mission_id
                WHERE c.tenant_id = :t AND c.device_id = :d AND c.id <> :me
                  AND m.source = 'mobile'
                  AND c.captured_at BETWEEN :earliest AND :latest
                  AND (c.latitude IS NULL OR CAST(:lat AS double precision) IS NULL
                       OR {_METRES} <= :reach)
                ORDER BY abs(extract(epoch FROM c.captured_at - CAST(:at AS timestamptz))),
                         c.captured_at
                LIMIT 1
                """),
            params,
        )
    ).scalar_one_or_none()
    if mission_id is None:
        mission_id = (
            await conn.execute(
                text("""
                    INSERT INTO missions (tenant_id, source, device_id, started_at,
                        name, site)
                    VALUES (:t, 'mobile', :d, :at, :name, :site)
                    RETURNING id
                    """),
                {
                    "t": tenant_id,
                    "d": device_id,
                    "at": captured_at,
                    "name": mission_name(captured_at, time_zone, place),
                    "site": (place or "").strip() or None,
                },
            )
        ).scalar_one()
    else:
        await conn.execute(
            text("""
                UPDATE missions SET started_at = least(started_at, :at)
                WHERE tenant_id = :t AND id = :m
                """),
            {"t": tenant_id, "m": mission_id, "at": captured_at},
        )
        await place_a_phone_mission(conn, tenant_id, mission_id, place)
    await conn.execute(
        text("UPDATE captures SET mission_id = :m WHERE tenant_id = :t AND id = :c"),
        {"t": tenant_id, "m": mission_id, "c": capture_id},
    )
    return mission_id
