"""The database side of the laser slice, tenant-scoped.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/src/fishsense_api/
controllers/: the laser cohorts (dive_cohort_controller.py
`select_next_for_laser_preprocessing`, `select_dives_needing_laser_population`,
`select_next_for_laser_auto_accept`; dive_prediction_cohort_controller.py
`select_next_for_laser_prediction`), the laser-label reads and writes
(label_controller.py: `get_laser_labels(include_superseded)`,
`get_dives_with_complete_laser_labeling`, `get_laser_label_studio_project_ids`
with `gated`, the label PUT), the reprocess flags
(label_reprocess_controller.py `_set_needs_reprocess`), the prediction upsert
(laser_prediction_controller.py) and the dive line (dive_controller.py) -- plus
the reads the v1 workers did client-side (the resolvers' image filters, the
populate/backfill/apply target selection, the validator's slate-label read).
The predicates are v1's; each resolver mirrors its cohort, canonical captures
only, or a dive is re-selected hourly with no work.

v2 changes:

* per tenant; each cohort orders by (created_at, number), so the orchestrator
  can take the oldest candidate across tenants and a migrated dive -- whose
  rows share one created_at -- keeps v1's lowest-id order;
* **the stage-0.1 and prediction cohorts ask for a camera**: a dive whose
  device has no current camera calibration is not a candidate. v1's cohorts
  did not ask, and its resolvers failed such a dive on every hourly run; with
  one oldest candidate across every tenant, that dive would starve them all;
* predictions are appended (0011), and "the dive's prediction for a capture" is
  its latest (`current_*`). **The gate's verdict is appended to
  laser_prediction_verdicts** (migration 0021) rather than written over the
  prediction, and only when it changed, as v1 wrote only changed rows;
* **the dive line is appended only when the fit changed** (dive_laser_lines is
  append-only; v1 rewrote it every hourly run for every complete dive);
* **populate never re-opens a label**: a task's row is recorded only where the
  (capture, project) has none. v1's PUT re-wrote an existing row to a fresh
  placeholder, which could un-supersede a label the validator had superseded;
* **the processor's output is checked** (PLAN.md §9.11): a prediction, verdict
  or supersede naming a row outside the dive is refused, and nothing is
  written;
* a revival (remediation) refuses if the dive's labels changed since the plan
  was made: the plan is judged on a snapshot the processor was handed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.camera_sql import RECTIFIABLE_CAMERA_MODEL, RECTIFIABLE_DIVE
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "LASER_PREDICTOR_VERSION",
    "DiveCamera",
    "ForeignRows",
    "GateInputs",
    "GateRow",
    "GateVerdict",
    "LabelPopulation",
    "LabelRow",
    "LaserCandidate",
    "LaserCapture",
    "LaserCatalog",
    "LaserPopulation",
    "LineFit",
    "NewLaserPrediction",
    "PopulateItem",
    "PopulatedLabel",
    "PopulationChanged",
    "TaskTarget",
    "TaskTargets",
    "ValidationWrite",
]

#: The laser-detector stage version the cohorts compare against. The contract
#: package owns it (`fishsense_services_contracts.laser`); the API does not
#: depend on the contract, so the number is repeated here and the
#: orchestrator's tests pin the two together.
LASER_PREDICTOR_VERSION = 2

#: v1's gate compares recorded margins loosely: a float round trip must not
#: manufacture a write.
_MARGIN_TOLERANCE = 1e-6


class ForeignRows(ValueError):
    """The processor named a row that is not the dive's; nothing was written."""


class PopulationChanged(RuntimeError):
    """The dive's labels changed since the plan was made; nothing was written."""


@dataclass(frozen=True)
class LaserCandidate:
    dive_id: uuid.UUID
    created_at: datetime
    #: v1's dive id for a migrated dive: the tiebreak of the cohort order.
    number: int


@dataclass(frozen=True)
class LaserCapture:
    capture_id: uuid.UUID
    #: v1's image id for a migrated capture.
    number: int
    checksum: str
    #: Migrated from v1 (has a `v1_id`): its JPEG may be where v1 wrote it.
    from_v1: bool
    captured_at: datetime


@dataclass(frozen=True)
class DiveCamera:
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]


@dataclass(frozen=True)
class NewLaserPrediction:
    """One processor result, as persisted (v1's `LaserPredictionResult`)."""

    capture_id: uuid.UUID
    confidence: float
    x: float | None = None
    y: float | None = None
    width: int | None = None
    height: int | None = None
    color: str | None = None
    color_margin: float | None = None
    rejected_out_of_region: bool = False
    predictor_version: int | None = None
    checkpoint: str | None = None
    core_version: str | None = None


@dataclass(frozen=True)
class GateRow:
    prediction_id: uuid.UUID
    capture_number: int
    x: float | None
    y: float | None
    predictor_version: int | None


@dataclass(frozen=True)
class GateInputs:
    dive_number: int
    predictions: list[GateRow]


@dataclass(frozen=True)
class GateVerdict:
    prediction_id: uuid.UUID
    auto_accept: bool
    gate_verdict: str
    line_offset_px: float | None
    line_position_z: float | None


@dataclass(frozen=True)
class PopulateItem:
    capture: LaserCapture
    x: float | None
    y: float | None
    width: int | None
    height: int | None
    #: The prediction's effective gate flag (its latest verdict's).
    auto_accept: bool


@dataclass(frozen=True)
class LaserPopulation:
    dive_number: int
    items: list[PopulateItem]
    #: Every current prediction's colour in the dive: the majority vote's input.
    colors: list[str | None]


@dataclass(frozen=True)
class PopulatedLabel:
    capture_id: uuid.UUID
    ls_project_id: int
    ls_task_id: int
    #: `human` (a task for a labeler) or `auto_accept` (imported annotated).
    source: str


@dataclass(frozen=True)
class TaskTarget:
    capture_id: uuid.UUID
    ls_task_id: int
    ls_project_id: int
    x: float
    y: float
    width: int | None
    height: int | None


@dataclass(frozen=True)
class TaskTargets:
    dive_number: int
    colors: list[str | None]
    targets: list[TaskTarget]


@dataclass(frozen=True)
class LabelRow:
    label_id: uuid.UUID
    number: int
    capture_number: int
    x: float
    y: float
    superseded: bool
    completed: bool


@dataclass(frozen=True)
class LabelPopulation:
    rows: list[LabelRow]
    calibration_capture_numbers: list[int]
    #: A digest of exactly what was read, so a later write can refuse if the
    #: dive changed in between.
    fingerprint: str


@dataclass(frozen=True)
class LineFit:
    a: float
    b: float
    c: float
    n_points: int
    inlier_count: int
    inlier_fraction: float
    residual_std: float
    label_noise_mad: float
    line_confidence: float
    noise_estimator: str


@dataclass(frozen=True)
class ValidationWrite:
    superseded: int
    line_appended: bool


# -- shared SQL ----------------------------------------------------------------

#: A canonical capture of dive `d`.
_CANONICAL = "c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical"

_ORDER = "ORDER BY d.created_at, d.number"

#: Dive `d` can be rectified, which the preprocess and predict resolvers need
#: (`dive_camera`). v2 only; see `camera_sql`.
_HAS_CAMERA = RECTIFIABLE_DIVE


async def _lock_dive(conn: AsyncConnection, what: str, dive_id: uuid.UUID) -> None:
    """Serialise writes of one kind to one dive."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"laser-{what}:{dive_id}"},
    )


async def _candidates(conn, tenant_id, where: str, *, limit: bool) -> list:
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.created_at, d.number FROM dives d
            WHERE d.tenant_id = :tenant AND d.priority = 'high' AND ({where})
            {_ORDER} {"LIMIT 1" if limit else ""}
            """),
        {"tenant": tenant_id, "version": LASER_PREDICTOR_VERSION},
    )
    return [LaserCandidate(r.id, r.created_at, r.number) for r in rows]


async def _dive_number(conn, tenant_id, dive_id) -> int:
    number = (
        await conn.execute(
            text("SELECT number FROM dives WHERE tenant_id = :t AND id = :d"),
            {"t": tenant_id, "d": dive_id},
        )
    ).scalar_one_or_none()
    if number is None:
        raise ForeignRows(f"tenant {tenant_id} has no dive {dive_id}")
    return number


def _capture(row) -> LaserCapture:
    return LaserCapture(
        capture_id=row.capture_id,
        number=row.capture_number,
        checksum=row.checksum,
        from_v1=row.from_v1,
        captured_at=row.captured_at,
    )


_CAPTURE_COLUMNS = (
    "c.id AS capture_id, c.number AS capture_number, c.checksum, "
    "c.v1_id IS NOT NULL AS from_v1, c.captured_at"
)


# -- stage 0.1: preprocess -------------------------------------------------------

#: Capture `c` needs its stage-0.1 JPEG: no non-sentinel label (a sentinel has
#: no project; an incomplete row seeded by populate counts as labeled), or a
#: live label flagged `needs_reprocess` (v1's two ways in).
_NEEDS_LASER_JPEG = """(
    NOT EXISTS (
        SELECT 1 FROM laser_labels l
        WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
          AND l.ls_project_id IS NOT NULL
    )
    OR EXISTS (
        SELECT 1 FROM laser_labels l
        WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
          AND l.needs_reprocess AND NOT l.superseded
    )
)"""


async def next_dive_for_laser_preprocessing(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> LaserCandidate | None:
    """Stage 0.1: the tenant's oldest high-priority dive with a canonical
    capture that needs its laser JPEG, and a camera to rectify it with."""
    found = await _candidates(
        conn,
        tenant_id,
        f"EXISTS (SELECT 1 FROM captures c WHERE {_CANONICAL} AND {_NEEDS_LASER_JPEG})"
        f" AND {_HAS_CAMERA}",
        limit=True,
    )
    return found[0] if found else None


async def laser_preprocess_captures(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[LaserCapture]:
    """The dive's canonical captures needing a laser JPEG -- the cohort's
    predicate, per capture."""
    rows = await conn.execute(
        text(f"""
            SELECT {_CAPTURE_COLUMNS} FROM captures c
            JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
            WHERE d.tenant_id = :tenant AND d.id = :dive AND {_CANONICAL}
              AND {_NEEDS_LASER_JPEG}
            ORDER BY c.captured_at, c.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [_capture(r) for r in rows]


async def dive_camera(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> DiveCamera | None:
    """The intrinsics that rectify the dive: its device's current camera
    calibration (v1: the dive's camera's intrinsics). None when the dive has
    no device, the device no calibration, or its calibration is not a
    pinhole (`camera_sql`)."""
    row = (
        await conn.execute(
            text("""
                SELECT cc.camera_matrix, cc.distortion_coefficients
                FROM dives d
                JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                WHERE d.tenant_id = :tenant AND d.id = :dive
                  AND cc.camera_model = :model
                """),
            {"tenant": tenant_id, "dive": dive_id, "model": RECTIFIABLE_CAMERA_MODEL},
        )
    ).one_or_none()
    if row is None:
        return None
    return DiveCamera(row.camera_matrix, list(row.distortion_coefficients))


async def raise_laser_reprocess_flags(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    only_incomplete: bool = True,
) -> int:
    """Flag the dive's laser labels for a stage-0.1 redraw (v1's PUT
    .../labels/laser/needs-reprocess): canonical captures, live rows only (a
    flag the resolver cannot see would wedge the cohort), and by default only
    incomplete ones. Idempotent."""
    updated = await conn.execute(
        text(f"""
            UPDATE laser_labels l SET needs_reprocess = true
            FROM captures c
            WHERE l.tenant_id = :tenant AND c.tenant_id = l.tenant_id
              AND c.id = l.capture_id AND c.dive_id = :dive AND c.is_canonical
              AND NOT l.superseded
              {"AND NOT l.completed" if only_incomplete else ""}
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return updated.rowcount


async def clear_laser_reprocess_flags(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    capture_ids: list[uuid.UUID] | None,
) -> int:
    """Lower the flag once the JPEGs are redrawn (v1's DELETE): canonical
    captures, whatever the row's state (a flag nothing lowers wedges its
    dive). `capture_ids` scopes it to the frames redrawn (v1 named them by
    checksum, which is one canonical capture per tenant); None clears the
    whole dive; [] clears nothing."""
    if capture_ids is not None and not capture_ids:
        return 0
    scope = "" if capture_ids is None else "AND c.id = ANY(:captures)"
    updated = await conn.execute(
        text(f"""
            UPDATE laser_labels l SET needs_reprocess = false
            FROM captures c
            WHERE l.tenant_id = :tenant AND c.tenant_id = l.tenant_id
              AND c.id = l.capture_id AND c.dive_id = :dive AND c.is_canonical
              AND l.needs_reprocess {scope}
            """),
        {"tenant": tenant_id, "dive": dive_id, "captures": capture_ids},
    )
    return updated.rowcount


# -- laser prediction --------------------------------------------------------------

#: Capture `c` has a completed label a human placed that is still live: the
#: detector never predicts over finished work.
_HAS_LIVE_COMPLETED_LABEL = """EXISTS (
    SELECT 1 FROM laser_labels l
    WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
      AND l.completed AND NOT l.superseded
)"""

#: Capture `c`'s current prediction: its latest.
_CURRENT_PREDICTION = """(
    SELECT p.predictor_version FROM laser_predictions p
    WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
    ORDER BY p.seq DESC LIMIT 1
)"""


async def next_dive_for_laser_prediction(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> LaserCandidate | None:
    """v1's two ways in: a canonical capture with no prediction and no live
    completed label; or, on a dive still being labeled, one whose current
    prediction is from another stage version (NULL counts as stale). v2: and
    a camera to rectify it with."""
    unpredicted = f"""EXISTS (
        SELECT 1 FROM captures c WHERE {_CANONICAL}
          AND NOT EXISTS (
              SELECT 1 FROM laser_predictions p
              WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
          )
          AND NOT {_HAS_LIVE_COMPLETED_LABEL}
    )"""
    being_labeled = f"""EXISTS (
        SELECT 1 FROM captures c
        JOIN laser_labels l ON l.tenant_id = c.tenant_id AND l.capture_id = c.id
        WHERE {_CANONICAL} AND NOT l.completed AND NOT l.superseded
          AND l.ls_project_id IS NOT NULL
    )"""
    stale = f"""EXISTS (
        SELECT 1 FROM captures c WHERE {_CANONICAL}
          AND EXISTS (
              SELECT 1 FROM laser_predictions p
              WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
          )
          AND {_CURRENT_PREDICTION} IS DISTINCT FROM :version
          AND NOT {_HAS_LIVE_COMPLETED_LABEL}
    )"""
    found = await _candidates(
        conn,
        tenant_id,
        f"({unpredicted} OR ({being_labeled} AND {stale})) AND {_HAS_CAMERA}",
        limit=True,
    )
    return found[0] if found else None


async def laser_predict_captures(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[LaserCapture]:
    """The dive's canonical captures whose current prediction is missing or
    stale, and that no human has finished labeling (v1's resolver; the
    dive-level "still being labeled" gate is deliberately the selector's
    alone)."""
    rows = await conn.execute(
        text(f"""
            SELECT {_CAPTURE_COLUMNS} FROM captures c
            JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
            WHERE d.tenant_id = :tenant AND d.id = :dive AND {_CANONICAL}
              AND {_CURRENT_PREDICTION} IS DISTINCT FROM :version
              AND NOT {_HAS_LIVE_COMPLETED_LABEL}
            ORDER BY c.captured_at, c.number
            """),
        {"tenant": tenant_id, "dive": dive_id, "version": LASER_PREDICTOR_VERSION},
    )
    return [_capture(r) for r in rows]


async def persist_laser_predictions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    predictions: list[NewLaserPrediction],
) -> int:
    """Append one prediction per result. It has no verdict of its own, so it
    reads as unjudged -- v1's "a re-prediction clears the verdict". Every
    capture must be the dive's, or nothing is written."""
    if not predictions:
        return 0
    captures = [p.capture_id for p in predictions]
    known = set(
        (
            await conn.execute(
                text("""
                    SELECT id FROM captures
                    WHERE tenant_id = :tenant AND dive_id = :dive
                      AND id = ANY(:ids)
                    """),
                {"tenant": tenant_id, "dive": dive_id, "ids": captures},
            )
        ).scalars()
    )
    if foreign := set(captures) - known:
        raise ForeignRows(
            f"not captures of dive {dive_id}: {sorted(map(str, foreign))}"
        )
    await conn.execute(
        text("""
            INSERT INTO laser_predictions
                (tenant_id, capture_id, x, y, confidence, width, height, color,
                 color_margin, rejected_out_of_region, predictor_version,
                 checkpoint, core_version)
            VALUES (:tenant, :capture_id, :x, :y, :confidence, :width, :height,
                    :color, :color_margin, :rejected_out_of_region,
                    :predictor_version, :checkpoint, :core_version)
            """),
        [
            {
                "tenant": tenant_id,
                "capture_id": p.capture_id,
                "x": p.x,
                "y": p.y,
                "confidence": p.confidence,
                "width": p.width,
                "height": p.height,
                "color": p.color,
                "color_margin": p.color_margin,
                "rejected_out_of_region": p.rejected_out_of_region,
                "predictor_version": p.predictor_version,
                "checkpoint": p.checkpoint,
                "core_version": p.core_version,
            }
            for p in predictions
        ],
    )
    return len(predictions)


# -- the auto-accept gate ------------------------------------------------------------


async def next_dive_for_laser_auto_accept(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> LaserCandidate | None:
    """The gate's backlog: a canonical capture whose current prediction carries
    a dot, is from the current stage version (`=`: NULL is excluded) and has
    no verdict. Abstentions are excluded, which is what lets it drain."""
    found = await _candidates(
        conn,
        tenant_id,
        f"""EXISTS (
            SELECT 1 FROM captures c
            JOIN current_laser_predictions_gated p
              ON p.tenant_id = c.tenant_id AND p.capture_id = c.id
            WHERE {_CANONICAL} AND p.x IS NOT NULL AND p.y IS NOT NULL
              AND p.gate_verdict IS NULL AND p.predictor_version = :version
        )""",
        limit=True,
    )
    return found[0] if found else None


async def laser_gate_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> GateInputs:
    """The dive's WHOLE current prediction set (v1: every prediction of the
    dive's images, canonical or not) -- the gate is a consensus of the dive."""
    number = await _dive_number(conn, tenant_id, dive_id)
    rows = await conn.execute(
        text("""
            SELECT p.id, c.number AS capture_number, p.x, p.y, p.predictor_version
            FROM current_laser_predictions_gated p
            JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive
            ORDER BY c.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return GateInputs(
        dive_number=number,
        predictions=[
            GateRow(r.id, r.capture_number, r.x, r.y, r.predictor_version) for r in rows
        ],
    )


def _margin_changed(stored: float | None, computed: float | None) -> bool:
    if (stored is None) != (computed is None):
        return True
    return stored is not None and abs(stored - computed) > _MARGIN_TOLERANCE


def _verdict_changed(row, verdict: GateVerdict) -> bool:
    """v1's `_changed`: does the prediction's standing verdict differ?"""
    return (
        row.auto_accept != verdict.auto_accept
        or row.gate_verdict != verdict.gate_verdict
        or _margin_changed(row.line_offset_px, verdict.line_offset_px)
        or _margin_changed(row.line_position_z, verdict.line_position_z)
    )


async def record_laser_gate_verdicts(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    verdicts: list[GateVerdict],
) -> int:
    """Append each verdict that differs from its prediction's standing one
    (its latest verdict, else a migrated row's own). Returns how many were
    appended. Every prediction must be one of the dive's."""
    if not verdicts:
        return 0
    await _lock_dive(conn, "gate", dive_id)
    ids = [v.prediction_id for v in verdicts]
    standing = {
        r.id: r
        for r in await conn.execute(
            text("""
                SELECT p.id,
                       coalesce(v.auto_accept, p.auto_accept) AS auto_accept,
                       CASE WHEN v.id IS NULL THEN p.gate_verdict
                            ELSE v.gate_verdict END AS gate_verdict,
                       CASE WHEN v.id IS NULL THEN p.line_offset_px
                            ELSE v.line_offset_px END AS line_offset_px,
                       CASE WHEN v.id IS NULL THEN p.line_position_z
                            ELSE v.line_position_z END AS line_position_z
                FROM laser_predictions p
                JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
                LEFT JOIN LATERAL (
                    SELECT * FROM laser_prediction_verdicts lv
                    WHERE lv.tenant_id = p.tenant_id AND lv.prediction_id = p.id
                    ORDER BY lv.seq DESC LIMIT 1
                ) v ON true
                WHERE p.tenant_id = :tenant AND c.dive_id = :dive
                  AND p.id = ANY(:ids)
                """),
            {"tenant": tenant_id, "dive": dive_id, "ids": ids},
        )
    }
    if foreign := set(ids) - set(standing):
        raise ForeignRows(
            f"not predictions of dive {dive_id}: {sorted(map(str, foreign))}"
        )
    changed = [v for v in verdicts if _verdict_changed(standing[v.prediction_id], v)]
    if changed:
        await conn.execute(
            text("""
                INSERT INTO laser_prediction_verdicts
                    (tenant_id, prediction_id, auto_accept, gate_verdict,
                     line_offset_px, line_position_z)
                VALUES (:tenant, :prediction, :auto_accept, :verdict, :offset, :z)
                """),
            [
                {
                    "tenant": tenant_id,
                    "prediction": v.prediction_id,
                    "auto_accept": v.auto_accept,
                    "verdict": v.gate_verdict,
                    "offset": v.line_offset_px,
                    "z": v.line_position_z,
                }
                for v in changed
            ],
        )
    return len(changed)


# -- Label Studio: populate, backfill, apply auto-accept -----------------------------


async def dives_needing_laser_population(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[LaserCandidate]:
    """Every high-priority dive with a canonical capture that has a prediction
    and no completed label (superseded not filtered, as in v1). Prediction-
    gated: populating before predict would starve the predict cohort."""
    return await _candidates(
        conn,
        tenant_id,
        f"""EXISTS (
            SELECT 1 FROM captures c WHERE {_CANONICAL}
              AND EXISTS (
                  SELECT 1 FROM laser_predictions p
                  WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM laser_labels l
                  WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
                    AND l.completed
              )
        )""",
        limit=False,
    )


async def _colors(conn, tenant_id, dive_id) -> list[str | None]:
    rows = await conn.execute(
        text("""
            SELECT p.color FROM current_laser_predictions_gated p
            JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return list(rows.scalars())


async def laser_populate_items(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> LaserPopulation:
    """The dive's canonical captures that need a laser task: a current
    prediction and no live completed label (v1's populate filter). Each with
    its prediction and effective gate flag, and every prediction's colour."""
    number = await _dive_number(conn, tenant_id, dive_id)
    rows = await conn.execute(
        text(f"""
            SELECT {_CAPTURE_COLUMNS}, p.x, p.y, p.width, p.height, p.auto_accept
            FROM captures c
            JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
            JOIN current_laser_predictions_gated p
              ON p.tenant_id = c.tenant_id AND p.capture_id = c.id
            WHERE d.tenant_id = :tenant AND d.id = :dive AND {_CANONICAL}
              AND NOT {_HAS_LIVE_COMPLETED_LABEL}
            ORDER BY c.captured_at, c.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    items = [
        PopulateItem(_capture(r), r.x, r.y, r.width, r.height, r.auto_accept)
        for r in rows
    ]
    return LaserPopulation(number, items, await _colors(conn, tenant_id, dive_id))


async def dive_has_laser_labels_in_project(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID, ls_project_id: int
) -> bool:
    """Whether the dive holds live labels in the project: populate publishes
    an already-complete project only if it has tasks (v1)."""
    return (
        await conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM laser_labels l
                    JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
                    WHERE l.tenant_id = :tenant AND c.dive_id = :dive
                      AND l.ls_project_id = :project AND NOT l.superseded
                )
                """),
            {"tenant": tenant_id, "dive": dive_id, "project": ls_project_id},
        )
    ).scalar_one()


async def record_populated_laser_labels(
    conn: AsyncConnection, tenant_id: uuid.UUID, labels: list[PopulatedLabel]
) -> int:
    """Record the row anchoring each (capture, task, project) -- only where the
    capture has none in that project. An existing row, live or superseded, is
    never re-written: the sync owns its state, and v1's re-write could
    un-supersede a label. Returns how many rows were written."""
    written = 0
    for label in labels:
        inserted = await conn.execute(
            text("""
                INSERT INTO laser_labels
                    (tenant_id, capture_id, ls_project_id, ls_task_id, source,
                     completed, superseded)
                VALUES (:tenant, :capture, :project, :task, :source, false, false)
                ON CONFLICT DO NOTHING
                """),
            {
                "tenant": tenant_id,
                "capture": label.capture_id,
                "project": label.ls_project_id,
                "task": label.ls_task_id,
                "source": label.source,
            },
        )
        written += inserted.rowcount
    return written


async def laser_task_targets(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    auto_accepted_only: bool = False,
) -> TaskTargets:
    """The dive's open Label Studio tasks with a placeable prediction: a live,
    incomplete label with a task, whose capture's current prediction has a dot
    (and, for the auto-accept apply, the gate's flag). First task per capture
    wins, in v1's (image, label) order."""
    number = await _dive_number(conn, tenant_id, dive_id)
    rows = await conn.execute(
        text(f"""
            SELECT DISTINCT ON (c.number)
                   c.id AS capture_id, l.ls_task_id, l.ls_project_id,
                   p.x, p.y, p.width, p.height
            FROM laser_labels l
            JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
            JOIN current_laser_predictions_gated p
              ON p.tenant_id = c.tenant_id AND p.capture_id = c.id
            WHERE l.tenant_id = :tenant AND c.dive_id = :dive
              AND NOT l.completed AND NOT l.superseded
              AND l.ls_task_id IS NOT NULL AND l.ls_project_id IS NOT NULL
              AND p.x IS NOT NULL AND p.y IS NOT NULL
              {"AND p.auto_accept" if auto_accepted_only else ""}
            ORDER BY c.number, l.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    targets = [
        TaskTarget(r.capture_id, r.ls_task_id, r.ls_project_id, r.x, r.y,
                   r.width, r.height)
        for r in rows
    ]  # fmt: skip
    return TaskTargets(number, await _colors(conn, tenant_id, dive_id), targets)


async def mark_laser_labels_auto_accepted(
    conn: AsyncConnection, tenant_id: uuid.UUID, ls_task_ids: list[int]
) -> int:
    """Record that the gate confirmed these tasks' labels (source
    `auto_accept`). The sync stays the single writer of x/y and `completed`."""
    if not ls_task_ids:
        return 0
    updated = await conn.execute(
        text("""
            UPDATE laser_labels SET source = 'auto_accept'
            WHERE tenant_id = :tenant AND ls_task_id = ANY(:tasks)
              AND NOT completed AND source IS DISTINCT FROM 'auto_accept'
            """),
        {"tenant": tenant_id, "tasks": ls_task_ids},
    )
    return updated.rowcount


async def laser_label_studio_project_ids(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    incomplete: bool = False,
    gated: bool | None = None,
) -> list[int]:
    """The projects of the tenant's live laser labels (v1's endpoint).
    `incomplete` narrows to projects with open work; `gated=True` to projects
    the gate has *finished* with (none of their live-labelled captures' current
    predictions unjudged, at least one judged); `gated=False` is its exact
    complement. For the web portal's landing page."""

    def scan(judged: bool) -> str:
        return f"""EXISTS (
            SELECT 1 FROM laser_labels i
            JOIN current_laser_predictions_gated p
              ON p.tenant_id = i.tenant_id AND p.capture_id = i.capture_id
            WHERE i.tenant_id = l.tenant_id AND i.ls_project_id = l.ls_project_id
              AND NOT i.superseded
              AND p.gate_verdict IS {"NOT NULL" if judged else "NULL"}
        )"""

    where = ["l.tenant_id = :tenant", "l.ls_project_id IS NOT NULL", "NOT l.superseded"]
    if incomplete:
        where.append("NOT l.completed")
    if gated is not None:
        finished = f"({scan(True)} AND NOT {scan(False)})"
        where.append(finished if gated else f"NOT {finished}")
    rows = await conn.execute(
        text(f"""
            SELECT DISTINCT l.ls_project_id FROM laser_labels l
            WHERE {" AND ".join(where)}
            ORDER BY l.ls_project_id
            """),
        {"tenant": tenant_id},
    )
    return list(rows.scalars())


# -- per-dive laser-label validation, and its remediation ------------------------------


async def dives_with_complete_laser_labeling(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[LaserCandidate]:
    """Dives whose laser labeling is complete: at least one live completed
    label and no live incomplete one (v1's validation cohort; no priority
    filter, as in v1)."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.created_at, d.number FROM dives d
            WHERE d.tenant_id = :tenant
              AND EXISTS (
                  SELECT 1 FROM laser_labels l
                  JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
                  WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                    AND l.completed AND NOT l.superseded
              )
              AND NOT EXISTS (
                  SELECT 1 FROM laser_labels l
                  JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
                  WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                    AND NOT l.completed AND NOT l.superseded
              )
            {_ORDER}
            """),
        {"tenant": tenant_id},
    )
    return [LaserCandidate(r.id, r.created_at, r.number) for r in rows]


_POPULATION = """
    SELECT l.id, l.number, c.number AS capture_number, l.x, l.y,
           l.superseded, l.completed
    FROM laser_labels l
    JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
    WHERE l.tenant_id = :tenant AND c.dive_id = :dive
      AND l.x IS NOT NULL AND l.y IS NOT NULL
    ORDER BY c.number, l.number
"""


def _fingerprint(rows: list[LabelRow], calibration: list[int]) -> str:
    canonical = [
        [r.number, r.capture_number, r.x, r.y, r.superseded, r.completed] for r in rows
    ]
    return hashlib.sha256(json.dumps([canonical, calibration]).encode()).hexdigest()


async def _calibration(conn, tenant_id, dive_id) -> list[int]:
    rows = await conn.execute(
        text("""
            SELECT DISTINCT c.number FROM slate_labels s
            JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
            WHERE s.tenant_id = :tenant AND c.dive_id = :dive
              AND s.completed AND NOT s.superseded
            ORDER BY c.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return list(rows.scalars())


async def laser_label_population(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, lock=False
) -> LabelPopulation:
    """The dive's full positive population, superseded included (#927), and
    its calibration frames: captures with a completed, live slate label --
    exactly what stage 13 reads. Rows with no dot are not part of it."""
    sql = _POPULATION + (" FOR UPDATE OF l" if lock else "")
    rows = [
        LabelRow(r.id, r.number, r.capture_number, r.x, r.y, r.superseded,
                 r.completed)
        for r in await conn.execute(text(sql), {"tenant": tenant_id, "dive": dive_id})
    ]  # fmt: skip
    calibration = await _calibration(conn, tenant_id, dive_id)
    return LabelPopulation(rows, calibration, _fingerprint(rows, calibration))


async def apply_laser_validation(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    supersedes: list[tuple[uuid.UUID, str]],
    line: LineFit | None,
) -> ValidationWrite:
    """Write one judgement: supersede each flagged row that is still live,
    recording the rule that took it, and append the dive's line if it
    changed. Never revives. Every label must be one of the dive's."""
    await _lock_dive(conn, "validation", dive_id)
    superseded = 0
    if supersedes:
        ids = [label_id for label_id, _ in supersedes]
        mine = set(
            (
                await conn.execute(
                    text("""
                        SELECT l.id FROM laser_labels l
                        JOIN captures c
                          ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
                        WHERE l.tenant_id = :tenant AND c.dive_id = :dive
                          AND l.id = ANY(:ids)
                        """),
                    {"tenant": tenant_id, "dive": dive_id, "ids": ids},
                )
            ).scalars()
        )
        if foreign := set(ids) - mine:
            raise ForeignRows(
                f"not laser labels of dive {dive_id}: {sorted(map(str, foreign))}"
            )
        for label_id, reason in supersedes:
            updated = await conn.execute(
                text("""
                    UPDATE laser_labels
                    SET superseded = true, superseded_reason = :reason
                    WHERE tenant_id = :tenant AND id = :id AND NOT superseded
                    """),
                {"tenant": tenant_id, "id": label_id, "reason": reason},
            )
            superseded += updated.rowcount
    appended = False
    if line is not None:
        appended = await _append_line_if_changed(conn, tenant_id, dive_id, line)
    return ValidationWrite(superseded=superseded, line_appended=appended)


_LINE_COLUMNS = (
    "a",
    "b",
    "c",
    "n_points",
    "inlier_count",
    "inlier_fraction",
    "residual_std",
    "label_noise_mad",
    "line_confidence",
    "noise_estimator",
)


async def _append_line_if_changed(conn, tenant_id, dive_id, line: LineFit) -> bool:
    columns = ", ".join(_LINE_COLUMNS)
    params = ", ".join(f":{c}" for c in _LINE_COLUMNS)
    same = " AND ".join(f"cur.{c} IS NOT DISTINCT FROM :{c}" for c in _LINE_COLUMNS)
    inserted = await conn.execute(
        text(f"""
            INSERT INTO dive_laser_lines (tenant_id, dive_id, {columns})
            SELECT :tenant, :dive, {params}
            WHERE NOT EXISTS (
                SELECT 1 FROM (
                    SELECT * FROM dive_laser_lines
                    WHERE tenant_id = :tenant AND dive_id = :dive
                    ORDER BY seq DESC LIMIT 1
                ) cur
                WHERE {same}
            )
            """),
        {"tenant": tenant_id, "dive": dive_id,
         **{c: getattr(line, c) for c in _LINE_COLUMNS}},
    )  # fmt: skip
    return inserted.rowcount > 0


async def revive_laser_labels(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    label_numbers: list[int],
    fingerprint: str,
) -> int:
    """Revive reviewed labels: superseded=False, reason `remediation` (on a
    live row it records the last change was a revival). Refuses, writing
    nothing, if the dive's population is not the one the plan was made from,
    or a label is not the dive's. Labels already live are skipped, so
    re-applying is a no-op. Returns how many were revived."""
    await _lock_dive(conn, "validation", dive_id)
    population = await laser_label_population(conn, tenant_id, dive_id, lock=True)
    by_number = {r.number: r for r in population.rows}
    if foreign := set(label_numbers) - set(by_number):
        raise ForeignRows(f"not laser labels of dive {dive_id}: {sorted(foreign)}")
    pending = [n for n in label_numbers if by_number[n].superseded]
    if not pending:
        return 0
    if population.fingerprint != fingerprint:
        raise PopulationChanged(
            f"dive {dive_id}'s laser labels changed since the plan was made"
        )
    updated = await conn.execute(
        text("""
            UPDATE laser_labels
            SET superseded = false, superseded_reason = 'remediation'
            WHERE tenant_id = :tenant AND number = ANY(:numbers)
              AND superseded AND completed
            """),
        {"tenant": tenant_id, "numbers": pending},
    )
    return updated.rowcount


async def dive_by_number(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> uuid.UUID | None:
    return (
        await conn.execute(
            text("SELECT id FROM dives WHERE tenant_id = :t AND number = :n"),
            {"t": tenant_id, "n": number},
        )
    ).scalar_one_or_none()


async def dive_numbers(conn: AsyncConnection, tenant_id: uuid.UUID) -> list[int]:
    rows = await conn.execute(
        text("SELECT number FROM dives WHERE tenant_id = :t ORDER BY number"),
        {"t": tenant_id},
    )
    return list(rows.scalars())


class LaserCatalog(ServicePrincipal):
    """The laser slice's database side, as the orchestrator's service principal.

    Each method is the store function of the same name, tenant first; each
    call re-checks the membership and runs in the tenant's own transaction.
    """

    async def _call(self, fn, tenant_id: uuid.UUID, *args, **kwargs):
        async with self._tenant(tenant_id) as conn:
            return await fn(conn, tenant_id, *args, **kwargs)

    async def next_dive_for_laser_preprocessing(self, tenant_id):
        return await self._call(next_dive_for_laser_preprocessing, tenant_id)

    async def laser_preprocess_captures(self, tenant_id, dive_id):
        return await self._call(laser_preprocess_captures, tenant_id, dive_id)

    async def dive_camera(self, tenant_id, dive_id):
        return await self._call(dive_camera, tenant_id, dive_id)

    async def raise_laser_reprocess_flags(self, tenant_id, dive_id, **kwargs):
        return await self._call(
            raise_laser_reprocess_flags, tenant_id, dive_id, **kwargs
        )

    async def clear_laser_reprocess_flags(self, tenant_id, dive_id, capture_ids):
        return await self._call(
            clear_laser_reprocess_flags, tenant_id, dive_id, capture_ids
        )

    async def next_dive_for_laser_prediction(self, tenant_id):
        return await self._call(next_dive_for_laser_prediction, tenant_id)

    async def laser_predict_captures(self, tenant_id, dive_id):
        return await self._call(laser_predict_captures, tenant_id, dive_id)

    async def persist_laser_predictions(self, tenant_id, dive_id, predictions):
        return await self._call(
            persist_laser_predictions, tenant_id, dive_id, predictions
        )

    async def next_dive_for_laser_auto_accept(self, tenant_id):
        return await self._call(next_dive_for_laser_auto_accept, tenant_id)

    async def laser_gate_inputs(self, tenant_id, dive_id):
        return await self._call(laser_gate_inputs, tenant_id, dive_id)

    async def record_laser_gate_verdicts(self, tenant_id, dive_id, verdicts):
        return await self._call(
            record_laser_gate_verdicts, tenant_id, dive_id, verdicts
        )

    async def dives_needing_laser_population(self, tenant_id):
        return await self._call(dives_needing_laser_population, tenant_id)

    async def laser_populate_items(self, tenant_id, dive_id):
        return await self._call(laser_populate_items, tenant_id, dive_id)

    async def dive_has_laser_labels_in_project(self, tenant_id, dive_id, project):
        return await self._call(
            dive_has_laser_labels_in_project, tenant_id, dive_id, project
        )

    async def record_populated_laser_labels(self, tenant_id, labels):
        return await self._call(record_populated_laser_labels, tenant_id, labels)

    async def laser_task_targets(self, tenant_id, dive_id, **kwargs):
        return await self._call(laser_task_targets, tenant_id, dive_id, **kwargs)

    async def mark_laser_labels_auto_accepted(self, tenant_id, ls_task_ids):
        return await self._call(mark_laser_labels_auto_accepted, tenant_id, ls_task_ids)

    async def laser_label_studio_project_ids(self, tenant_id, **kwargs):
        return await self._call(laser_label_studio_project_ids, tenant_id, **kwargs)

    async def dives_with_complete_laser_labeling(self, tenant_id):
        return await self._call(dives_with_complete_laser_labeling, tenant_id)

    async def laser_label_population(self, tenant_id, dive_id):
        return await self._call(laser_label_population, tenant_id, dive_id)

    async def apply_laser_validation(self, tenant_id, dive_id, supersedes, line):
        return await self._call(
            apply_laser_validation, tenant_id, dive_id, supersedes, line
        )

    async def revive_laser_labels(self, tenant_id, dive_id, numbers, fingerprint):
        return await self._call(
            revive_laser_labels, tenant_id, dive_id, numbers, fingerprint
        )

    async def dive_by_number(self, tenant_id, number):
        return await self._call(dive_by_number, tenant_id, number)

    async def dive_numbers(self, tenant_id):
        return await self._call(dive_numbers, tenant_id)
