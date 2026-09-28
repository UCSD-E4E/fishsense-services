"""What the range-trend audit reads for one dive, tenant-scoped. Read-only.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/scripts/audit_length_range_trend.py (`audit_dive`), which read
through the SDK: the dive's resolved laser extrinsics, its measurements, its
laser depths with `depth_m > 0`, and its completed, non-superseded species
labels parsed with `parse_model_name`. v2 reads each from its home:

* the resolved extrinsics are the dive's `effective_laser_calibrations` row
  (migration 0018: its own accepted, plausible calibration, else its source
  dive's), and the baseline is `hypot(laser_position[:2])`, as in v1;
* the measurements are the *current* server measurements (0028's
  `current_measurements`) on canonical captures -- what v1 had left after its
  stale-binding DELETE;
* a capture's depth is its `current_laser_depths` row;
* a capture's object name is `fish_model_name` (0026: `parse_model_name` in
  SQL, pinned to it by tests/test_depth_measure_schema.py) of its latest
  completed, non-superseded species label. v1 kept whichever label its dict
  saw last; v2 takes the highest-numbered, which is v1's id for a migrated
  label.

A wild fish is named by its `fish_number`, v2's public identifier (v1's id for
a migrated fish).
"""

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["RangeTrendInputs", "RangeTrendMeasurement", "range_trend_inputs"]


@dataclass(frozen=True)
class RangeTrendMeasurement:
    capture_id: uuid.UUID
    length_m: float | None
    fish_number: int | None


@dataclass(frozen=True)
class RangeTrendInputs:
    """One dive's inputs to `range_trend.group_by_object` and `range_trend`."""

    #: The effective laser calibration's position; None when the dive
    #: resolves to no usable calibration.
    laser_position: list[float] | None
    measurements: list[RangeTrendMeasurement]
    depth_by_capture: dict[uuid.UUID, float]
    #: The rigid-target name of each measured capture's live species label
    #: (None: a real fish, or a label naming no rigid target).
    name_by_capture: dict[uuid.UUID, str | None]

    @property
    def baseline_m(self) -> float | None:
        if self.laser_position is None:
            return None
        return math.hypot(*self.laser_position[:2])


async def range_trend_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_number: int
) -> RangeTrendInputs | None:
    """The audit's inputs for the tenant's dive `dive_number`, or None when
    the tenant has no such dive."""
    dive = (
        await conn.execute(
            text("""
                SELECT d.id, lc.laser_position
                FROM dives d
                LEFT JOIN effective_laser_calibrations e
                    ON e.tenant_id = d.tenant_id AND e.dive_id = d.id
                LEFT JOIN laser_calibrations lc
                    ON lc.tenant_id = e.tenant_id AND lc.id = e.laser_calibration_id
                WHERE d.tenant_id = :tenant AND d.number = :number
                """),
            {"tenant": tenant_id, "number": dive_number},
        )
    ).one_or_none()
    if dive is None:
        return None

    rows = await conn.execute(
        text("""
            SELECT m.capture_id, m.length_m, f.number AS fish_number,
                   ld.depth_m, sl.content_of_image IS NOT NULL AS labelled,
                   fish_model_name(sl.content_of_image) AS name
            FROM current_measurements m
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            LEFT JOIN fish f ON f.tenant_id = m.tenant_id AND f.id = m.fish_id
            LEFT JOIN current_laser_depths ld
                ON ld.tenant_id = m.tenant_id AND ld.capture_id = m.capture_id
               AND ld.depth_m > 0
            LEFT JOIN LATERAL (
                SELECT s.content_of_image FROM species_labels s
                WHERE s.tenant_id = m.tenant_id AND s.capture_id = m.capture_id
                  AND s.completed AND NOT s.superseded
                ORDER BY s.number DESC
                LIMIT 1
            ) sl ON true
            WHERE m.tenant_id = :tenant AND c.dive_id = :dive
              AND c.is_canonical AND m.source = 'server'
            ORDER BY m.number
            """),
        {"tenant": tenant_id, "dive": dive.id},
    )
    measurements, depths, names = [], {}, {}
    for r in rows:
        measurements.append(
            RangeTrendMeasurement(r.capture_id, r.length_m, r.fish_number)
        )
        if r.depth_m is not None:
            depths[r.capture_id] = r.depth_m
        if r.labelled:
            names[r.capture_id] = r.name
    return RangeTrendInputs(
        laser_position=(
            None if dive.laser_position is None else list(dive.laser_position)
        ),
        measurements=measurements,
        depth_by_capture=depths,
        name_by_capture=names,
    )
