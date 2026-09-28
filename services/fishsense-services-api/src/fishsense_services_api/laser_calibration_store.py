"""The database side of stage 13 and checkerboard laser calibration.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/src/fishsense_api/
controllers/dive_cohort_controller.py (`select_next_for_laser_calibration`,
`select_next_for_checkerboard_laser_calibration`, `_has_live_laser_dot`,
`_usable_slate_observation_count`, `_stage_13_can_calibrate`,
`_calibration_refusal_still_stands`, `_reentry_last`,
`implausible_calibration_dive_ids`, `MIN_SLATE_LASER_POINTS`) and
dive_controller.py (`put_laser_extrinsics_for_dive`, `set_calibration_refused`,
`clear_calibration_refused`, `_newest_label_timestamp`, `_clear_refusal`); the
reads v1's stage-13 data-worker activity made through the SDK
(perform_laser_calibration_activity.py); and
services/fishsense-api-workflow-worker/src/.../activities/
resolve_checkerboard_calibration_inputs_activity.py.

v1's rules, kept:

* **stage 13** offers a HIGH dive with no usable calibration of its own, a
  slate template, and at least `MIN_SLATE_LASER_POINTS` *observations* -- a
  completed, live slate label on a canonical frame that also carries a live
  laser dot (live: not superseded, x and y set; nothing about `completed`).
  Counting labels instead wedged prod on dive 347;
* **the checkerboard** offers a HIGH dive with a calibration target, no usable
  calibration of its own (no borrowed fallback), at least that many canonical
  frames with a live dot, and -- so the two cohorts partition -- one stage 13
  cannot calibrate;
* a stored fit with an implausible baseline is no calibration, and such a
  dive re-enters **last**, so a repeat refusal cannot head-of-line block a
  healthy dive;
* **a refusal holds the dive out** until something changes: a laser or slate
  label on it, in any state, newer (in Label Studio's clock) than the labels
  the refusal was computed from -- a missing snapshot means any label is
  newer;
* the checkerboard resolver emits one frame per canonical image with a live
  dot, the lowest label winning, and carries the board's geometry.

v2 changes:

* per tenant; candidates oldest first (`created_at`), re-entry last, so the
  orchestrator can take the oldest across the tenants it serves;
* **a refusal is a row**: the dive's current `laser_calibrations` row is
  `refused`, so it stands only while nothing newer was appended. v1's
  `_clear_refusal` ran on a successful fit (here an accepted row simply
  becomes current), on `set_dive_slate` / `set_calibration_target` (here the
  refusal records the template or board version it used, and a dive whose
  link now differs is no longer refused -- so a board's pitch correction,
  a new version row, expires it too), and on the operator's DELETE (here an
  appended `laser_calibration_refusal_clears` row). Carried-over v1 refusals
  recorded no target and expire on labels or a clear only;
* `inputs_as_of` is snapshotted **when the inputs are resolved**, not when the
  refusal is recorded: a label synced while the fit ran is newer than what
  the fit saw, and now expires the refusal (v1 snapshotted at refusal time
  and missed it);
* **no upsert**: a refit is appended, so it is visible to every
  provenance-mismatch cohort (v1's PUT kept the row id);
* stage 13's observations take each frame's lowest live laser label (v1:
  `get_laser_label(image_id).first()`, no ordering), and the board's geometry
  is read through `current_calibration_targets` by name -- per axis -- so a
  pitch correction reaches the next fit, which records the version it used.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "MIN_SLATE_LASER_POINTS",
    "BoardFrame",
    "CalibrationCandidate",
    "CalibrationInputsUnavailable",
    "CalibrationRecord",
    "CheckerboardInputs",
    "LaserCalibrationCatalog",
    "SlateCalibrationInputs",
    "SlateObservationRow",
    "checkerboard_calibration_inputs",
    "clear_calibration_refusal",
    "next_dive_for_checkerboard_calibration",
    "next_dive_for_laser_calibration",
    "record_laser_calibration",
    "slate_calibration_inputs",
]

#: The fit's `MIN_LASER_POINTS` (`fishsense_services_contracts.calibration_
#: bounds`), spelled for SQL because the API image does not install the
#: contracts; a test pins the two. One threshold on both sides of the worker
#: boundary: a dive between them is re-selected hourly with nothing written.
MIN_SLATE_LASER_POINTS = 2


class CalibrationInputsUnavailable(ValueError):
    """A dive a calibration cannot be resolved for: the reason is in the message."""


@dataclass(frozen=True)
class CalibrationCandidate:
    dive_id: uuid.UUID
    created_at: datetime
    #: Its latest accepted fit is implausible: offered after everything else.
    reentry: bool


@dataclass(frozen=True)
class SlateObservationRow:
    capture_id: uuid.UUID
    reference_points: list[tuple[float, float]] | None
    skipped_points: list[int] | None
    laser_x: float
    laser_y: float


@dataclass(frozen=True)
class SlateCalibrationInputs:
    dive_id: uuid.UUID
    slate_template_id: uuid.UUID
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    template_points: list[tuple[float, float]]
    dpi: int
    observations: list[SlateObservationRow]
    dive_dots: list[tuple[float, float]]
    #: The newest laser or slate label on the dive, in Label Studio's clock.
    inputs_as_of: datetime | None


@dataclass(frozen=True)
class BoardFrame:
    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    laser_x: float
    laser_y: float


@dataclass(frozen=True)
class CheckerboardInputs:
    dive_id: uuid.UUID
    #: The *current* version of the dive's target, which the fit records.
    calibration_target_id: uuid.UUID
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    #: Interior corners.
    rows: int
    cols: int
    pitch_x_m: float
    pitch_y_m: float
    frames: list[BoardFrame]
    dive_dots: list[tuple[float, float]]
    inputs_as_of: datetime | None


@dataclass(frozen=True)
class CalibrationRecord:
    """One attempt, as `laser_calibrations` stores it."""

    producer: str
    outcome: str
    laser_position: list[float] | None
    laser_axis: list[float] | None
    refusal_reason: str | None
    gate_verdicts: dict[str, Any] | None
    lever_arm_m: float | None
    observation_count: int | None
    core_version: str | None
    camera_calibration_id: uuid.UUID | None
    slate_template_id: uuid.UUID | None
    calibration_target_id: uuid.UUID | None
    inputs_as_of: datetime | None


# --- the SQL both cohorts share (each over a dive aliased `d`) -----------------

#: A live laser dot on capture `c`: what the fit keeps (v1's `get_laser_label`).
_LIVE_DOT = """
    EXISTS (
        SELECT 1 FROM laser_labels l
        WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
          AND NOT l.superseded AND l.x IS NOT NULL AND l.y IS NOT NULL
    )
"""

#: The dive's current row is an accepted, plausible fit of its own.
_HAS_OWN_CALIBRATION = """
    EXISTS (
        SELECT 1 FROM current_laser_calibrations cur
        WHERE cur.tenant_id = d.tenant_id AND cur.dive_id = d.id
          AND cur.outcome = 'accepted'
          AND plausible_laser_baseline(cur.laser_position)
    )
"""

#: Its latest accepted fit is implausible (v1's `implausible_calibration_
#: dive_ids`): a re-entry candidate, offered last.
_REENTRY = """
    coalesce((
        SELECT NOT plausible_laser_baseline(a.laser_position)
        FROM laser_calibrations a
        WHERE a.tenant_id = d.tenant_id AND a.dive_id = d.id
          AND a.outcome = 'accepted'
        ORDER BY a.seq DESC LIMIT 1
    ), false)
"""

#: v1's `_usable_slate_observation_count`.
_SLATE_OBSERVATIONS = f"""
    (SELECT count(*) FROM slate_labels s
     JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
     WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
       AND s.completed AND NOT s.superseded AND {_LIVE_DOT})
"""

#: v1's `_stage_13_can_calibrate`: what makes the two cohorts partition.
_STAGE_13_CAN_CALIBRATE = f"""
    (d.slate_template_id IS NOT NULL
     AND {_SLATE_OBSERVATIONS} >= {MIN_SLATE_LASER_POINTS})
"""

#: The current version of the dive's calibration target, by name.
_EFFECTIVE_TARGET = """
    (SELECT cur.id FROM calibration_targets t
     JOIN current_calibration_targets cur ON cur.name = t.name
     WHERE t.id = d.calibration_target_id)
"""

#: A label on the dive (any state) newer than refusal `r`'s inputs.
_NEWER_LABEL = """
    EXISTS (
        SELECT 1 FROM {table} x
        JOIN captures c ON c.tenant_id = x.tenant_id AND c.id = x.capture_id
        WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
          AND (r.inputs_as_of IS NULL OR x.ls_updated_at > r.inputs_as_of)
    )
"""

#: v1's `_calibration_refusal_still_stands`, over append-only rows. The
#: dive's latest row is read from the table, not `current_laser_calibrations`:
#: that view was created with `SELECT *` in 0008, so Postgres froze its column
#: list before `v1_refusal_dive_id` (0016) and the target columns existed.
_REFUSAL_STANDS = f"""
    EXISTS (
        SELECT 1 FROM (
            SELECT * FROM laser_calibrations z
            WHERE z.tenant_id = d.tenant_id AND z.dive_id = d.id
            ORDER BY z.seq DESC LIMIT 1
        ) r
        WHERE r.outcome = 'refused'
          AND NOT EXISTS (
              SELECT 1 FROM laser_calibration_refusal_clears k
              WHERE k.tenant_id = r.tenant_id AND k.laser_calibration_id = r.id
          )
          AND (r.v1_refusal_dive_id IS NOT NULL OR (
                (r.producer IS DISTINCT FROM 'slate'
                 OR r.slate_template_id IS NOT DISTINCT FROM d.slate_template_id)
            AND (r.producer IS DISTINCT FROM 'checkerboard'
                 OR r.calibration_target_id IS NOT DISTINCT FROM {_EFFECTIVE_TARGET})
          ))
          AND NOT {_NEWER_LABEL.format(table="laser_labels")}
          AND NOT {_NEWER_LABEL.format(table="slate_labels")}
    )
"""


async def _next(conn, tenant_id, where: str) -> CalibrationCandidate | None:
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at, {_REENTRY} AS reentry FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND NOT {_HAS_OWN_CALIBRATION}
                  AND {where}
                  AND NOT {_REFUSAL_STANDS}
                ORDER BY reentry, d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id},
        )
    ).one_or_none()
    return (
        None
        if row is None
        else CalibrationCandidate(row.id, row.created_at, bool(row.reentry))
    )


async def next_dive_for_laser_calibration(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> CalibrationCandidate | None:
    """The tenant's next dive in the stage-13 (slate) cohort."""
    return await _next(conn, tenant_id, _STAGE_13_CAN_CALIBRATE)


async def next_dive_for_checkerboard_calibration(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> CalibrationCandidate | None:
    """The tenant's next dive in the checkerboard cohort."""
    return await _next(
        conn,
        tenant_id,
        f"""
        d.calibration_target_id IS NOT NULL
        AND NOT {_STAGE_13_CAN_CALIBRATE}
        AND (SELECT count(*) FROM captures c
             WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
               AND c.is_canonical AND {_LIVE_DOT}) >= {MIN_SLATE_LASER_POINTS}
        """,
    )


# --- inputs ---------------------------------------------------------------------


async def _dive_and_camera(conn, tenant_id, dive_id):
    row = (
        await conn.execute(
            text(f"""
                SELECT d.slate_template_id, {_EFFECTIVE_TARGET} AS target_id,
                       cc.id AS camera_calibration_id, cc.camera_matrix,
                       cc.distortion_coefficients
                FROM dives d
                LEFT JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                WHERE d.tenant_id = :tenant AND d.id = :dive
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).one_or_none()
    if row is None:
        raise CalibrationInputsUnavailable(f"dive_id={dive_id} not found")
    return row


def _require_camera(row, dive_id) -> None:
    if row.camera_calibration_id is None:
        raise CalibrationInputsUnavailable(
            f"dive_id={dive_id} has no camera calibration for its device"
        )


async def _dive_dots(conn, tenant_id, dive_id) -> list[tuple[float, float]]:
    """Every live dot in the dive (incomplete and non-canonical included)."""
    rows = await conn.execute(
        text("""
            SELECT l.x, l.y FROM laser_labels l
            JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
            WHERE l.tenant_id = :tenant AND c.dive_id = :dive
              AND NOT l.superseded AND l.x IS NOT NULL AND l.y IS NOT NULL
            ORDER BY c.number, l.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [(float(r.x), float(r.y)) for r in rows]


async def _inputs_as_of(conn, tenant_id, dive_id) -> datetime | None:
    """The newest laser or slate label on the dive, any state (v1's
    `_newest_label_timestamp`), in Label Studio's clock."""
    return (
        await conn.execute(
            text("""
                SELECT max(at) FROM (
                    SELECT x.ls_updated_at AS at FROM laser_labels x
                    JOIN captures c ON c.tenant_id = x.tenant_id AND c.id = x.capture_id
                    WHERE x.tenant_id = :tenant AND c.dive_id = :dive
                    UNION ALL
                    SELECT x.ls_updated_at FROM slate_labels x
                    JOIN captures c ON c.tenant_id = x.tenant_id AND c.id = x.capture_id
                    WHERE x.tenant_id = :tenant AND c.dive_id = :dive
                ) labels
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).scalar_one()


#: Each capture's lowest live laser label (by number, v1's id order).
_LOWEST_LIVE_DOT = """
    SELECT DISTINCT ON (l.capture_id) l.capture_id, l.x, l.y
    FROM laser_labels l
    WHERE l.tenant_id = :tenant AND NOT l.superseded
      AND l.x IS NOT NULL AND l.y IS NOT NULL
    ORDER BY l.capture_id, l.number
"""


async def slate_calibration_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> SlateCalibrationInputs | None:
    """What stage 13's fit reads; None when the dive has no slate template or
    no live slate labels (v1's no-op)."""
    setup = await _dive_and_camera(conn, tenant_id, dive_id)
    if setup.slate_template_id is None:
        return None
    template = (
        await conn.execute(
            text("SELECT dpi, reference_points FROM slate_templates WHERE id = :id"),
            {"id": setup.slate_template_id},
        )
    ).one()
    labels = (
        await conn.execute(
            text(f"""
                SELECT s.capture_id, s.reference_points, s.skipped_points,
                       dot.x, dot.y
                FROM slate_labels s
                JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
                LEFT JOIN ({_LOWEST_LIVE_DOT}) dot ON dot.capture_id = s.capture_id
                WHERE s.tenant_id = :tenant AND c.dive_id = :dive
                  AND NOT s.superseded
                ORDER BY c.number, s.number
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).all()
    if not labels:
        return None
    _require_camera(setup, dive_id)
    if template.dpi is None or not template.reference_points:
        raise CalibrationInputsUnavailable(
            f"slate_template_id={setup.slate_template_id} missing dpi or "
            "reference_points"
        )
    return SlateCalibrationInputs(
        dive_id=dive_id,
        slate_template_id=setup.slate_template_id,
        camera_calibration_id=setup.camera_calibration_id,
        camera_matrix=setup.camera_matrix,
        template_points=[tuple(p) for p in template.reference_points],
        dpi=template.dpi,
        observations=[
            SlateObservationRow(
                capture_id=row.capture_id,
                reference_points=(
                    None
                    if row.reference_points is None
                    else [tuple(p) for p in row.reference_points]
                ),
                skipped_points=row.skipped_points,
                laser_x=float(row.x),
                laser_y=float(row.y),
            )
            for row in labels
            if row.x is not None
        ],
        dive_dots=await _dive_dots(conn, tenant_id, dive_id),
        inputs_as_of=await _inputs_as_of(conn, tenant_id, dive_id),
    )


async def checkerboard_calibration_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> CheckerboardInputs:
    """What the checkerboard fit (and the lattice study) reads: the current
    version of the dive's board, its camera, and one frame per canonical
    image with a live dot."""
    setup = await _dive_and_camera(conn, tenant_id, dive_id)
    if setup.target_id is None:
        raise CalibrationInputsUnavailable(
            f"dive_id={dive_id} has no calibration target"
        )
    _require_camera(setup, dive_id)
    board = (
        await conn.execute(
            text("""
                SELECT interior_rows, interior_cols, pitch_x_m, pitch_y_m
                FROM calibration_targets WHERE id = :id
                """),
            {"id": setup.target_id},
        )
    ).one()
    frames = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1, dot.x, dot.y
            FROM captures c
            JOIN ({_LOWEST_LIVE_DOT}) dot ON dot.capture_id = c.id
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive AND c.is_canonical
            ORDER BY c.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return CheckerboardInputs(
        dive_id=dive_id,
        calibration_target_id=setup.target_id,
        camera_calibration_id=setup.camera_calibration_id,
        camera_matrix=setup.camera_matrix,
        distortion_coefficients=setup.distortion_coefficients,
        rows=board.interior_rows,
        cols=board.interior_cols,
        pitch_x_m=board.pitch_x_m,
        pitch_y_m=board.pitch_y_m,
        frames=[
            BoardFrame(r.id, r.checksum, r.from_v1, float(r.x), float(r.y))
            for r in frames
        ],
        dive_dots=await _dive_dots(conn, tenant_id, dive_id),
        inputs_as_of=await _inputs_as_of(conn, tenant_id, dive_id),
    )


# --- writes ---------------------------------------------------------------------


async def record_laser_calibration(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    record: CalibrationRecord,
) -> uuid.UUID:
    """Append one attempt to the dive's calibrations. Returns its id."""
    return (
        await conn.execute(
            text("""
                INSERT INTO laser_calibrations
                    (tenant_id, dive_id, camera_calibration_id, producer, outcome,
                     laser_position, laser_axis, refusal_reason, inputs_as_of,
                     gate_verdicts, lever_arm_m, observation_count, core_version,
                     slate_template_id, calibration_target_id)
                VALUES (:tenant, :dive, :camera, :producer, :outcome,
                        CAST(:position AS jsonb), CAST(:axis AS jsonb), :reason,
                        :inputs_as_of, CAST(:verdicts AS jsonb), :lever, :count,
                        :core, :slate, :target)
                RETURNING id
                """),
            {
                "tenant": tenant_id,
                "dive": dive_id,
                "camera": record.camera_calibration_id,
                "producer": record.producer,
                "outcome": record.outcome,
                "position": _json(record.laser_position),
                "axis": _json(record.laser_axis),
                "reason": record.refusal_reason,
                "inputs_as_of": record.inputs_as_of,
                "verdicts": _json(record.gate_verdicts),
                "lever": record.lever_arm_m,
                "count": record.observation_count,
                "core": record.core_version,
                "slate": record.slate_template_id,
                "target": record.calibration_target_id,
            },
        )
    ).scalar_one()


def _json(value) -> str | None:
    return None if value is None else json.dumps(value)


async def clear_calibration_refusal(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> bool:
    """Clear the dive's current refusal, if one stands uncleared: the
    operator's "try it again" (v1's `DELETE /dives/{id}/calibration-refused/`).
    Idempotent; False when there was nothing to clear."""
    written = await conn.execute(
        text("""
            INSERT INTO laser_calibration_refusal_clears
                (tenant_id, laser_calibration_id, reason)
            SELECT r.tenant_id, r.id, :reason FROM current_laser_calibrations r
            WHERE r.tenant_id = :tenant AND r.dive_id = :dive
              AND r.outcome = 'refused'
            ON CONFLICT ON CONSTRAINT laser_calibration_refusal_clears_refusal_key
                DO NOTHING
            """),
        {"tenant": tenant_id, "dive": dive_id, "reason": reason},
    )
    return written.rowcount > 0


class LaserCalibrationCatalog(ServicePrincipal):
    """Laser calibration's database side, as the orchestrator's principal."""

    async def next_dive_for_laser_calibration(
        self, tenant_id: uuid.UUID
    ) -> CalibrationCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_laser_calibration(conn, tenant_id)

    async def next_dive_for_checkerboard_calibration(
        self, tenant_id: uuid.UUID
    ) -> CalibrationCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_checkerboard_calibration(conn, tenant_id)

    async def slate_calibration_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SlateCalibrationInputs | None:
        async with self._tenant(tenant_id) as conn:
            return await slate_calibration_inputs(conn, tenant_id, dive_id)

    async def checkerboard_calibration_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> CheckerboardInputs:
        async with self._tenant(tenant_id) as conn:
            return await checkerboard_calibration_inputs(conn, tenant_id, dive_id)

    async def record_laser_calibration(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, record: CalibrationRecord
    ) -> uuid.UUID:
        async with self._tenant(tenant_id) as conn:
            return await record_laser_calibration(conn, tenant_id, dive_id, record)

    async def clear_calibration_refusal(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, reason: str | None = None
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await clear_calibration_refusal(
                conn, tenant_id, dive_id, reason=reason
            )
