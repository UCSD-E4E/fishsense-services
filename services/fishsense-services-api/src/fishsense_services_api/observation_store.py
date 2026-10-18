"""Observations in the database: every measured fish, Lite and Mobile alike.

Reads the ``observations`` view (migration 0045) inside a tenant transaction,
so RLS scopes every row to that tenant; the queries also name the tenant, the
app layer's own line of defence (as every store does).

* :func:`list_observations` -- newest first, a page at a time, optionally
  only some device kinds.
* :func:`observation` -- one fish in one capture, with the evidence its
  method rests on: for ``laser``, the laser depth, the calibration's producer
  and the labelled dot, head and tail; for ``lidar``, the phone's snout and
  fork and who placed them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mobile_capture_store import _DERIVED_PLACE_NAME

__all__ = [
    "DevicePoints",
    "LaserEvidence",
    "Observation",
    "ObservationDetail",
    "list_observations",
    "observation",
]

Point = tuple[float, float]


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
    dive_number: int | None
    dive_name: str | None
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
    #: The capture's image in its tenant's bucket (Mobile only, for now).
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
    o.device_kind, o.device_name, o.dive_number, o.dive_name,
    o.scientific_name, o.common_name, o.fish_model, o.known_length_m,
    o.latitude, o.longitude, o.place_name, pn.place_name AS derived_place_name,
    o.client_capture_uuid, o.photo_key"""

#: ``_DERIVED_PLACE_NAME`` joins on capture ``c``.
_CAPTURE = "JOIN captures c ON c.tenant_id = o.tenant_id AND c.id = o.capture_id"


async def list_observations(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    limit: int,
    before: tuple[datetime, uuid.UUID, uuid.UUID] | None = None,
    device_kinds: Sequence[str] | None = None,
) -> list[Observation]:
    """Newest first; ``before`` is the previous page's last ``cursor``;
    ``device_kinds`` keeps only those kinds (None: every kind)."""
    rows = await conn.execute(
        text(f"""
            SELECT {_COLUMNS}
            FROM observations o
            {_CAPTURE}
            {_DERIVED_PLACE_NAME}
            WHERE o.tenant_id = :tenant_id
              AND (CAST(:kinds AS text[]) IS NULL
                   OR o.device_kind = ANY(CAST(:kinds AS text[])))
              AND (CAST(:before_at AS timestamptz) IS NULL
                   OR (o.captured_at, o.capture_id, o.fish_id)
                      < (:before_at, :before_capture, :before_fish))
            ORDER BY o.captured_at DESC, o.capture_id DESC, o.fish_id DESC
            LIMIT :limit
            """),
        {
            "tenant_id": tenant_id,
            "kinds": list(device_kinds) if device_kinds is not None else None,
            "before_at": before[0] if before else None,
            "before_capture": before[1] if before else None,
            "before_fish": before[2] if before else None,
            "limit": limit,
        },
    )
    return [_observation(row) for row in rows]


async def observation(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    fish_id: uuid.UUID,
) -> ObservationDetail | None:
    """One fish in one capture, with its evidence; None if there is no
    current measurement of it in this tenant."""
    row = (
        await conn.execute(
            text(f"""
                SELECT {_COLUMNS},
                    COALESCE(m.measured_at, m.created_at) AS measured,
                    m.algorithm, m.algorithm_version, m.core_version,
                    m.model_version,
                    depth.depth_m, depth.range_m, cal.producer,
                    ll.x AS dot_x, ll.y AS dot_y,
                    ht.head_x, ht.head_y, ht.tail_x, ht.tail_y,
                    dp.snout_x, dp.snout_y, dp.fork_x, dp.fork_y, dp.origin
                FROM observations o
                {_CAPTURE}
                {_DERIVED_PLACE_NAME}
                JOIN measurements m
                  ON m.tenant_id = o.tenant_id AND m.id = o.measurement_id
                LEFT JOIN laser_calibrations cal
                  ON cal.tenant_id = m.tenant_id AND cal.id = m.laser_calibration_id
                LEFT JOIN laser_labels ll
                  ON ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
                LEFT JOIN head_tail_labels ht
                  ON ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
                -- The depth the length used: named, else the latest from the
                -- same dot under the same calibration.
                LEFT JOIN LATERAL (
                    SELECT ld.depth_m, ld.range_m FROM laser_depths ld
                    WHERE ld.tenant_id = m.tenant_id
                      AND (ld.id = m.laser_depth_id
                           OR (m.laser_depth_id IS NULL
                               AND ld.capture_id = m.capture_id
                               AND ld.laser_label_id = m.laser_label_id
                               AND ld.laser_calibration_id = m.laser_calibration_id))
                    ORDER BY ld.id = m.laser_depth_id DESC, ld.created_at DESC
                    LIMIT 1
                ) depth ON m.source = 'server'
                LEFT JOIN device_measurement_points dp
                  ON dp.tenant_id = m.tenant_id AND dp.measurement_id = m.id
                WHERE o.tenant_id = :tenant_id
                  AND o.capture_id = :capture_id AND o.fish_id = :fish_id
                """),
            {"tenant_id": tenant_id, "capture_id": capture_id, "fish_id": fish_id},
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
        dive_number=row.dive_number,
        dive_name=row.dive_name,
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
