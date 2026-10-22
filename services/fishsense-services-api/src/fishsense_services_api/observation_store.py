"""Observations in the database: every measured fish, Lite and Mobile alike.

Reads the ``observations`` view (migration 0045) inside a tenant transaction,
so RLS scopes every row to that tenant; the queries also name the tenant, the
app layer's own line of defence (as every store does).

* :func:`list_observations` -- newest first, a page at a time, optionally
  one device group: ``lite``, ``mobile`` or ``other`` (any other kind, or no
  device), which together cover every fish once. :func:`observation_page` is
  the same with how many there are in all, for pages by number.
* :func:`observation` -- one fish in one capture, with the evidence its
  method rests on: for ``laser``, the laser depth, the calibration's producer
  and the labelled dot, head and tail; for ``lidar``, the phone's snout and
  fork and who placed them.

**The photo** (Phase 4 of §9.26). A mobile capture's is its own image; a Lite
capture's is its render -- the raw rectified to a JPEG, in the tenant's own
bucket (0051) -- the newest one rendered, whatever its version or camera: an
abstention after it (a raw gone from scratch on a later version) leaves it
standing. Only a render in the tenant's current bucket counts (the caller
names it): a presign for another bucket is a link to nothing. A Lite frame
not rendered there has no photo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.renders_store import newest_render
from fishsense_services_api.thumbnails_store import THUMBNAIL_WIDTH

__all__ = [
    "DEVICE_GROUPS",
    "DevicePoints",
    "LaserEvidence",
    "Observation",
    "ObservationDetail",
    "CaptureCard",
    "ObservationPage",
    "list_observations",
    "mission_captures",
    "observation",
    "observation_page",
]

Point = tuple[float, float]

#: The device kinds of each named group; ``other`` is every kind not named
#: here, and a fish with no device.
DEVICE_GROUPS = {"lite": ("lite", "lite_flatport"), "mobile": ("mobile",)}
_NAMED_KINDS = [kind for kinds in DEVICE_GROUPS.values() for kind in kinds]


@dataclass(frozen=True)
class Observation:
    capture_id: uuid.UUID
    fish_id: uuid.UUID
    #: ``laser`` (Lite) or ``lidar`` (Mobile): how the depth was found.
    method: str
    length_m: float | None
    captured_at: datetime
    device_kind: str | None
    device_name: str | None
    mission_number: int | None
    mission_name: str | None
    scientific_name: str | None
    common_name: str | None
    fish_model: str | None
    #: The fish model's current known length, when the fish is a model.
    known_length_m: float | None
    latitude: float | None
    longitude: float | None
    place_name: str | None
    #: The name the server derived from the coordinates (0041), if any.
    derived_place_name: str | None
    #: A mobile capture's own uuid (its page is ``/captures``).
    client_capture_uuid: uuid.UUID | None
    #: The capture's image in its tenant's bucket: a phone's own, else the
    #: capture's newest render there (0051) -- its thumbnail in a list (0053).
    photo_key: str | None

    @property
    def cursor(self) -> tuple[datetime, uuid.UUID, uuid.UUID]:
        """Where the next page starts (pass as ``before``)."""
        return (self.captured_at, self.capture_id, self.fish_id)


@dataclass(frozen=True)
class LaserEvidence:
    depth_m: float | None
    range_m: float | None
    calibration_producer: str | None
    #: In the labelled frame's pixels, like the head and tail.
    dot: Point | None
    head: Point | None
    tail: Point | None


@dataclass(frozen=True)
class DevicePoints:
    snout: Point
    fork: Point
    #: ``auto`` (the phone found them), ``manual``, or ``unknown``.
    origin: str


@dataclass(frozen=True)
class ObservationDetail:
    observation: Observation
    measured_at: datetime
    algorithm: str | None
    algorithm_version: str | None
    core_version: str | None
    model_version: str | None
    laser: LaserEvidence | None
    device_points: DevicePoints | None


_COLUMNS = """o.capture_id, o.fish_id, o.method, o.length_m, o.captured_at,
    o.device_kind, o.device_name, o.mission_number, o.mission_name,
    o.scientific_name, o.common_name, o.fish_model, o.known_length_m,
    o.latitude, o.longitude, o.place_name, pn.place_name AS derived_place_name,
    o.client_capture_uuid"""

#: The capture's newest render (0051) in the tenant's current bucket, for a
#: frame with no image of its own: `renders_store.newest_render`'s rule.
_RENDER = f"""LEFT JOIN LATERAL (
    {newest_render("o.capture_id", "o.tenant_id", "r.bucket = :photo_bucket")}
) r ON true"""

#: The card: the thumbnail (0053) of the capture's newest render that has one
#: made at the cards' width, in the same bucket -- so a re-render waiting for
#: its thumbnail does not blank the card (review of the stack). Never the full
#: render (review of #151).
_THUMBNAIL = f"""LEFT JOIN LATERAL (
    SELECT h.key FROM capture_thumbnails h
    JOIN capture_renders rr ON rr.tenant_id = h.tenant_id AND rr.id = h.render_id
    WHERE h.tenant_id = o.tenant_id AND h.capture_id = o.capture_id
      AND h.status = 'made' AND h.target_width = {THUMBNAIL_WIDTH}
      AND h.bucket = :photo_bucket
    ORDER BY rr.seq DESC LIMIT 1
) h ON true"""

#: The name the server derived from the capture's coordinates (0041).
_DERIVED_PLACE_NAME = """LEFT JOIN LATERAL (
    SELECT place_name FROM capture_place_names
    WHERE tenant_id = o.tenant_id AND capture_id = o.capture_id
    ORDER BY derived_at DESC LIMIT 1
) pn ON true"""


@dataclass(frozen=True)
class ObservationPage:
    observations: list[Observation]
    #: How many the filters keep in all, whatever the page or cursor.
    total: int


async def list_observations(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    limit: int,
    before: tuple[datetime, uuid.UUID, uuid.UUID] | None = None,
    device: str | None = None,
    photo_bucket: str | None = None,
    mission: int | None = None,
) -> list[Observation]:
    """Newest first; ``before`` is the previous page's last ``cursor``;
    ``device`` keeps one of :data:`DEVICE_GROUPS` or ``other`` (None: all);
    ``mission`` keeps one mission's, by number (phase 7b: how a phone's fish
    are reached once /captures is retired);
    ``photo_bucket`` is the tenant's current bucket (None: no Lite photos)."""
    found = await observation_page(
        conn,
        tenant_id,
        limit=limit,
        before=before,
        device=device,
        photo_bucket=photo_bucket,
        mission=mission,
    )
    return found.observations


async def observation_page(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    limit: int,
    offset: int = 0,
    before: tuple[datetime, uuid.UUID, uuid.UUID] | None = None,
    device: str | None = None,
    photo_bucket: str | None = None,
    mission: int | None = None,
) -> ObservationPage:
    """:func:`list_observations`, from the ``offset``-th, with the total.

    The total is a window over what the filters keep, counted in the same
    pass: the view is the cost of any read (it builds every current
    measurement in the tenant to sort them), so a second query for the count
    would pay it twice. The cursor applies after the count, so the total
    ignores it, as it does the page."""
    if device is not None and device != "other" and device not in DEVICE_GROUPS:
        raise ValueError(f"no device group {device!r}")
    params = {
        "tenant_id": tenant_id,
        "device": device,
        "mission": mission,
        "named": _NAMED_KINDS,
        "kinds": list(DEVICE_GROUPS.get(device, ())),
        "before_at": before[0] if before else None,
        "before_capture": before[1] if before else None,
        "before_fish": before[2] if before else None,
        "limit": limit,
        "offset": offset,
        "photo_bucket": photo_bucket,
    }
    # The page first, then its place names and renders: a lateral before
    # the LIMIT would probe every observation in the tenant (review of #151).
    rows = (
        await conn.execute(
            text(f"""
            SELECT {_COLUMNS}, coalesce(o.photo_key, h.key) AS photo_key, o.total
            FROM (
                SELECT * FROM ({_KEPT}) o
                WHERE CAST(:before_at AS timestamptz) IS NULL
                   OR (o.captured_at, o.capture_id, o.fish_id)
                      < (:before_at, :before_capture, :before_fish)
                ORDER BY o.captured_at DESC, o.capture_id DESC, o.fish_id DESC
                LIMIT :limit OFFSET :offset
            ) o
            {_DERIVED_PLACE_NAME}
            {_RENDER}
            {_THUMBNAIL}
            ORDER BY o.captured_at DESC, o.capture_id DESC, o.fish_id DESC
            """),
            params,
        )
    ).all()
    if rows:
        total = rows[0].total
    elif offset == 0 and before is None:
        total = 0
    else:
        # Past the end: no row to carry the count, so count alone.
        total = (
            await conn.execute(text(f"SELECT count(*) FROM ({_KEPT}) o"), params)
        ).scalar_one()
    return ObservationPage([_observation(row) for row in rows], total)


#: The tenant's observations that the device and mission filters keep, each
#: with how many there are in all.
_KEPT = """
    SELECT o.*, count(*) OVER () AS total FROM observations o
    WHERE o.tenant_id = :tenant_id
      AND (CAST(:device AS text) IS NULL
           OR (:device = 'other'
               AND (o.device_kind IS NULL
                    OR o.device_kind <> ALL(CAST(:named AS text[]))))
           OR o.device_kind = ANY(CAST(:kinds AS text[])))
      AND (CAST(:mission AS integer) IS NULL OR o.mission_number = :mission)
"""


async def observation(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    fish_id: uuid.UUID,
    *,
    photo_bucket: str | None = None,
) -> ObservationDetail | None:
    """One fish in one capture, with its evidence; None if there is no
    current measurement of it in this tenant."""
    row = (
        await conn.execute(
            text(f"""
                SELECT {_COLUMNS}, coalesce(o.photo_key, r.key) AS photo_key,
                    COALESCE(m.measured_at, m.created_at) AS measured,
                    m.algorithm, m.algorithm_version, m.core_version,
                    m.model_version,
                    depth.depth_m, depth.range_m, cal.producer,
                    ll.x AS dot_x, ll.y AS dot_y,
                    ht.head_x, ht.head_y, ht.tail_x, ht.tail_y,
                    dp.snout_x, dp.snout_y, dp.fork_x, dp.fork_y, dp.origin
                FROM observations o
                {_DERIVED_PLACE_NAME}
                {_RENDER}
                JOIN measurements m
                  ON m.tenant_id = o.tenant_id AND m.id = o.measurement_id
                LEFT JOIN laser_calibrations cal
                  ON cal.tenant_id = m.tenant_id AND cal.id = m.laser_calibration_id
                LEFT JOIN laser_labels ll
                  ON ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
                LEFT JOIN head_tail_labels ht
                  ON ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
                -- The depth the length used: named, else the latest written
                -- from the same dot under the same calibration (seq, as
                -- current_laser_depths has it: rows written in one transaction
                -- share a created_at).
                LEFT JOIN LATERAL (
                    SELECT ld.depth_m, ld.range_m FROM laser_depths ld
                    WHERE ld.tenant_id = m.tenant_id
                      AND (ld.id = m.laser_depth_id
                           OR (m.laser_depth_id IS NULL
                               AND ld.capture_id = m.capture_id
                               AND ld.laser_label_id = m.laser_label_id
                               AND ld.laser_calibration_id = m.laser_calibration_id))
                    ORDER BY ld.id = m.laser_depth_id DESC, ld.seq DESC
                    LIMIT 1
                ) depth ON m.source = 'server'
                LEFT JOIN device_measurement_points dp
                  ON dp.tenant_id = m.tenant_id AND dp.measurement_id = m.id
                WHERE o.tenant_id = :tenant_id
                  AND o.capture_id = :capture_id AND o.fish_id = :fish_id
                """),
            {
                "tenant_id": tenant_id,
                "capture_id": capture_id,
                "fish_id": fish_id,
                "photo_bucket": photo_bucket,
            },
        )
    ).first()
    if row is None:
        return None
    laser = None
    if row.method == "laser":
        laser = LaserEvidence(
            depth_m=row.depth_m,
            range_m=row.range_m,
            calibration_producer=row.producer,
            dot=_point(row.dot_x, row.dot_y),
            head=_point(row.head_x, row.head_y),
            tail=_point(row.tail_x, row.tail_y),
        )
    points = None
    if row.snout_x is not None:
        points = DevicePoints(
            snout=(row.snout_x, row.snout_y),
            fork=(row.fork_x, row.fork_y),
            origin=row.origin,
        )
    return ObservationDetail(
        observation=_observation(row),
        measured_at=row.measured,
        algorithm=row.algorithm,
        algorithm_version=row.algorithm_version,
        core_version=row.core_version,
        model_version=row.model_version,
        laser=laser,
        device_points=points,
    )


def _point(x: float | None, y: float | None) -> Point | None:
    return None if x is None or y is None else (x, y)


def _observation(row) -> Observation:
    return Observation(
        capture_id=row.capture_id,
        fish_id=row.fish_id,
        method=row.method,
        length_m=row.length_m,
        captured_at=row.captured_at,
        device_kind=row.device_kind,
        device_name=row.device_name,
        mission_number=row.mission_number,
        mission_name=row.mission_name,
        scientific_name=row.scientific_name,
        common_name=row.common_name,
        fish_model=row.fish_model,
        known_length_m=row.known_length_m,
        latitude=row.latitude,
        longitude=row.longitude,
        place_name=row.place_name,
        derived_place_name=row.derived_place_name,
        client_capture_uuid=row.client_capture_uuid,
        photo_key=row.photo_key,
    )


@dataclass(frozen=True)
class CaptureCard:
    """One capture of a mission, measured or not (2026-10-21)."""

    capture_id: uuid.UUID
    client_capture_uuid: uuid.UUID | None
    captured_at: datetime
    #: A phone's own JPEG; a Lite frame's render thumbnail; None: no image yet.
    photo_key: str | None
    #: Its measured fish, ``(fish_id, length_m)``; empty when none is measured.
    fish: list[tuple[uuid.UUID, float]]

    @property
    def cursor(self) -> tuple[datetime, uuid.UUID]:
        """Where the next page starts (pass as ``after``)."""
        return (self.captured_at, self.capture_id)


async def mission_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_number: int,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None = None,
    photo_bucket: str | None = None,
) -> list[CaptureCard] | None:
    """The mission's canonical captures in the order they were taken, each
    with its photo and measured fish; None for no such mission in the tenant.

    The Fish list is measured fish only, so a capture that was never measured
    is seen here or nowhere (docs/design/README.md: a capture is every frame,
    measured or not)."""
    mission_id = (
        await conn.execute(
            text("SELECT id FROM missions WHERE tenant_id = :t AND number = :n"),
            {"t": tenant_id, "n": mission_number},
        )
    ).scalar_one_or_none()
    if mission_id is None:
        return None
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.client_capture_uuid, c.captured_at,
                   CASE WHEN c.client_capture_uuid IS NOT NULL
                        THEN c.raw_object_key ELSE h.key END AS photo_key,
                   coalesce((
                       SELECT json_agg(json_build_array(o.fish_id, o.length_m)
                                       ORDER BY o.fish_id)
                       FROM observations o
                       WHERE o.tenant_id = c.tenant_id AND o.capture_id = c.id
                   ), '[]'::json) AS fish
            FROM captures c
            LEFT JOIN LATERAL (
                SELECT h.key FROM capture_thumbnails h
                JOIN capture_renders rr ON rr.tenant_id = h.tenant_id AND rr.id = h.render_id
                WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                  AND h.status = 'made' AND h.target_width = {THUMBNAIL_WIDTH}
                  AND h.bucket = :photo_bucket
                ORDER BY rr.seq DESC LIMIT 1
            ) h ON true
            WHERE c.tenant_id = :t AND c.mission_id = :m AND c.is_canonical
              AND (CAST(:after_at AS timestamptz) IS NULL
                   OR (c.captured_at, c.id) > (:after_at, :after_id))
            ORDER BY c.captured_at, c.id
            LIMIT :limit
            """),
        {
            "t": tenant_id,
            "m": mission_id,
            "photo_bucket": photo_bucket,
            "after_at": after[0] if after else None,
            "after_id": after[1] if after else None,
            "limit": limit,
        },
    )
    return [
        CaptureCard(
            capture_id=r.id,
            client_capture_uuid=r.client_capture_uuid,
            captured_at=r.captured_at,
            photo_key=r.photo_key,
            fish=[(uuid.UUID(f), length) for f, length in r.fish],
        )
        for r in rows
    ]
