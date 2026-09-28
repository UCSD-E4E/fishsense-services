"""The database side of the head/tail stages, tenant-scoped.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api (the cohorts in
controllers/dive_cohort_controller.py `select_next_for_headtail_preprocessing`
and dive_prediction_cohort_controller.py `select_next_for_headtail_prediction`
/ `select_dives_needing_headtail_population`; the flags in
label_reprocess_controller.py `_set_needs_reprocess`; the prediction write in
head_tail_prediction_controller.py) and services/fishsense-api-workflow-worker
(the inputs its activities assembled client-side:
resolve_headtail_preprocess_inputs_activity.py,
resolve_headtail_predict_inputs_activity.py `select_images_needing_prediction`,
and the reads and writes of populate_headtail_label_studio_project_activity.py
and backfill_headtail_predictions_activity.py).

v1's rules, kept:

* a *valid* laser label (completed, not superseded, x and y set) on a
  canonical capture is where head/tail starts;
* **resolvers mirror their selectors exactly**, the reprocess-flag branch
  included (no laser gate for a flagged frame), or a dive re-fires every hour;
* stage 5.1's "done" is a head/tail row *with a project* that is not
  superseded (a project-less row is a sentinel); the predict cohort's
  "labelled" is `completed`, not the project (populate seeds rows with one);
* a stale prediction is a version mismatch (`IS DISTINCT FROM`, so NULL is
  stale) or one made from a laser since superseded; never-predicted dives go
  first, so the fallback tier's permanently stale rows can't starve new work;
* the populate cohort is prediction-gated (an abstention opens it) and holds a
  dive until its labels are complete;
* raising a flag touches canonical, live and (by default) incomplete rows;
  clearing touches every canonical row, scoped to the frames redrawn -- None
  is the whole dive, [] is nothing.

v2 changes:

* per tenant, ordered by `created_at` (v1: `id`) so the orchestrator can take
  the oldest candidate across the tenants it serves;
* **predictions are appended** (migration 0011): v1's upsert on the image is
  an INSERT, and every reader judges the current one;
* intrinsics are the dive's device's current camera calibration, and a
  non-pinhole one is refused rather than rectified as a pinhole;
* **the processor's output is checked** (PLAN.md §9.11): predictions for
  another dive's captures, or naming another capture's laser label, are
  refused, and nothing is written;
* **populate never erases a labeler's work**: re-recording a task on an
  existing (capture, project) row sets the task and revives the row, and
  leaves the columns the sync owns alone. v1 PUT the whole row back blank.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.clustering_store import VALID_LASER
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "CurrentHeadTailPrediction",
    "ForeignCapture",
    "ForeignLaserLabel",
    "HeadTailPredictionRow",
    "HeadtailCandidate",
    "HeadtailCatalog",
    "HeadtailPreprocessInputs",
    "InvalidPredictions",
    "LaserDot",
    "LiveHeadTailLabel",
    "PopulateCandidate",
    "PopulateState",
    "PredictCapture",
    "PredictionCandidate",
    "PreprocessCapture",
    "UnsupportedCameraModel",
    "clear_headtail_needs_reprocess",
    "dives_needing_headtail_population",
    "headtail_populate_state",
    "headtail_predict_captures",
    "headtail_preprocess_inputs",
    "next_dive_for_headtail_prediction",
    "next_dive_for_headtail_preprocessing",
    "persist_headtail_predictions",
    "record_head_tail_task",
    "set_headtail_needs_reprocess",
    "supersede_head_tail_labels",
]

#: A live laser dot on capture `c`: the crop centre and the gate.
_LIVE_LASER = f"""EXISTS (
    SELECT 1 FROM laser_labels l
    WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id AND {VALID_LASER}
)"""

#: A human head/tail label on capture `c`: completed and not dead-lettered.
_LIVE_LABEL = """EXISTS (
    SELECT 1 FROM head_tail_labels h
    WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
      AND h.completed AND NOT h.superseded
)"""

#: Any prediction ever made for capture `c`, abstentions included.
_ANY_PREDICTION = """EXISTS (
    SELECT 1 FROM head_tail_predictions p
    WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
)"""


class UnsupportedCameraModel(ValueError):
    """Stage 5.1 rectifies with pinhole maths; this camera isn't one."""


class InvalidPredictions(ValueError):
    """The processor's predictions don't fit the dive they were made for."""


class ForeignCapture(InvalidPredictions):
    """A prediction for a capture that is not the dive's."""


class ForeignLaserLabel(InvalidPredictions):
    """A prediction naming a laser label that is not its capture's."""


@dataclass(frozen=True)
class HeadtailCandidate:
    dive_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class PredictionCandidate:
    dive_id: uuid.UUID
    created_at: datetime
    #: Some image has never been predicted (not merely a stale row).
    never_predicted: bool


@dataclass(frozen=True)
class PreprocessCapture:
    capture_id: uuid.UUID
    checksum: str
    #: Migrated from v1: its JPEG may be where v1 wrote it.
    from_v1: bool


@dataclass(frozen=True)
class HeadtailPreprocessInputs:
    captures: list[PreprocessCapture]
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]


@dataclass(frozen=True)
class LaserDot:
    laser_label_id: uuid.UUID
    x: float
    y: float


@dataclass(frozen=True)
class PredictCapture:
    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    #: Every live dot, in label order: first is the crop centre.
    dots: tuple[LaserDot, ...]
    has_existing_prediction: bool
    existing_laser_superseded: bool


@dataclass(frozen=True)
class HeadTailPredictionRow:
    """One prediction to append (the processor's result, mapped by the
    orchestrator; this package does not import the processing contract)."""

    capture_id: uuid.UUID
    status: str
    head_x: float | None = None
    head_y: float | None = None
    tail_x: float | None = None
    tail_y: float | None = None
    width: int | None = None
    height: int | None = None
    mask_area_px: int | None = None
    silhouette_ratio: float | None = None
    crop_x: int | None = None
    crop_y: int | None = None
    laser_label_id: uuid.UUID | None = None
    predictor_version: int | None = None
    checkpoint: str | None = None
    core_version: str | None = None


@dataclass(frozen=True)
class PopulateCandidate:
    """A capture with a valid laser and no completed live head/tail label."""

    capture_id: uuid.UUID
    #: v1's image id for a migrated capture: the task's `image_id`.
    number: int
    checksum: str
    from_v1: bool
    captured_at: datetime


@dataclass(frozen=True)
class CurrentHeadTailPrediction:
    capture_id: uuid.UUID
    status: str
    head_x: float | None
    head_y: float | None
    tail_x: float | None
    tail_y: float | None
    width: int | None
    height: int | None
    silhouette_ratio: float | None
    rejected_low_confidence: bool
    predictor_version: int | None


@dataclass(frozen=True)
class LiveHeadTailLabel:
    id: uuid.UUID
    capture_id: uuid.UUID
    ls_project_id: int | None
    ls_task_id: int | None
    completed: bool


@dataclass(frozen=True)
class PopulateState:
    """One snapshot of a dive, as populate and the backfill read it."""

    #: v1's dive id for a migrated dive: what `#{n}` in its titles is.
    dive_number: int
    candidates: list[PopulateCandidate]
    #: The current prediction of each of the dive's captures.
    predictions: list[CurrentHeadTailPrediction]
    #: The dive's live (not superseded) head/tail labels, every project.
    labels: list[LiveHeadTailLabel]


# -- stage 5.1 ----------------------------------------------------------------------


async def next_dive_for_headtail_preprocessing(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> HeadtailCandidate | None:
    """The tenant's oldest dive in the stage-5.1 cohort: a canonical capture
    with a valid laser and no live head/tail row in a project, or a canonical
    capture whose live row is flagged for a redraw."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND EXISTS (
                      SELECT 1 FROM captures c
                      WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                        AND c.is_canonical
                        AND (
                            ({_LIVE_LASER} AND NOT EXISTS (
                                SELECT 1 FROM head_tail_labels h
                                WHERE h.tenant_id = c.tenant_id
                                  AND h.capture_id = c.id
                                  AND h.ls_project_id IS NOT NULL
                                  AND NOT h.superseded
                            ))
                            OR EXISTS (
                                SELECT 1 FROM head_tail_labels h
                                WHERE h.tenant_id = c.tenant_id
                                  AND h.capture_id = c.id
                                  AND h.needs_reprocess AND NOT h.superseded
                            )
                        )
                  )
                ORDER BY d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id},
        )
    ).one_or_none()
    return None if row is None else HeadtailCandidate(row.id, row.created_at)


def _flat(values) -> list[float]:
    """A distortion vector as the flat list the contract takes: a one-row
    (1, N) or one-column matrix is flattened, as fishsense-core does."""
    if values and all(isinstance(v, list) for v in values):
        return [float(x) for row in values for x in row]
    return [float(x) for x in values]


async def headtail_preprocess_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> HeadtailPreprocessInputs:
    """The dive's frames to render and the intrinsics to rectify them with.

    Mirrors the cohort: canonical captures with a valid laser and no live row
    in a project, in laser-label order, then flagged frames (no laser gate) in
    capture order. Raises if the dive, its device or its calibration is
    missing, as v1 did for the dive, its camera and its intrinsics.
    """
    dive = (
        await conn.execute(
            text("SELECT device_id FROM dives WHERE tenant_id = :t AND id = :d"),
            {"t": tenant_id, "d": dive_id},
        )
    ).one_or_none()
    if dive is None:
        raise ValueError(f"dive {dive_id} not found")
    if dive.device_id is None:
        raise ValueError(f"dive {dive_id} has no device")
    calibration = (
        await conn.execute(
            text("""
                SELECT camera_model, camera_matrix, distortion_coefficients
                FROM current_camera_calibrations
                WHERE tenant_id = :t AND device_id = :device
                """),
            {"t": tenant_id, "device": dive.device_id},
        )
    ).one_or_none()
    if calibration is None:
        raise ValueError(f"device {dive.device_id} has no camera calibration")
    if calibration.camera_model != "pinhole":
        raise UnsupportedCameraModel(
            f"device {dive.device_id}'s camera is {calibration.camera_model!r}; "
            "stage 5.1 rectifies only a pinhole camera"
        )

    rows = (
        await conn.execute(
            text(f"""
                SELECT c.id, c.number, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                       (SELECT min(l.number) FROM laser_labels l
                        WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
                          AND {VALID_LASER}) AS first_laser,
                       EXISTS (
                           SELECT 1 FROM head_tail_labels h
                           WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                             AND h.ls_project_id IS NOT NULL AND NOT h.superseded
                       ) AS labelled,
                       EXISTS (
                           SELECT 1 FROM head_tail_labels h
                           WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                             AND h.needs_reprocess AND NOT h.superseded
                       ) AS flagged
                FROM captures c
                WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
                """),
            {"t": tenant_id, "d": dive_id},
        )
    ).all()
    eligible = sorted(
        (r for r in rows if r.first_laser is not None and not r.labelled),
        key=lambda r: r.first_laser,
    )
    seen = {r.id for r in eligible}
    flagged = sorted(
        (r for r in rows if r.flagged and r.id not in seen), key=lambda r: r.number
    )
    return HeadtailPreprocessInputs(
        captures=[
            PreprocessCapture(r.id, r.checksum, r.from_v1) for r in eligible + flagged
        ],
        camera_matrix=[[float(x) for x in row] for row in calibration.camera_matrix],
        distortion_coefficients=_flat(calibration.distortion_coefficients),
    )


async def _set_flags(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    value: bool,
    *,
    only_live: bool,
    only_incomplete: bool,
    checksums: Sequence[str] | None,
) -> int:
    conditions = ""
    if only_live:
        conditions += " AND NOT h.superseded"
    if only_incomplete:
        conditions += " AND NOT h.completed"
    if checksums is not None:
        conditions += " AND c.checksum = ANY(CAST(:checksums AS text[]))"
    result = await conn.execute(
        text(f"""
            UPDATE head_tail_labels h SET needs_reprocess = :value
            FROM captures c
            WHERE c.tenant_id = h.tenant_id AND c.id = h.capture_id
              AND h.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              {conditions}
            """),
        {"t": tenant_id, "d": dive_id, "value": value,
         "checksums": None if checksums is None else list(checksums)},
    )  # fmt: skip
    return result.rowcount


async def set_headtail_needs_reprocess(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    only_incomplete: bool = True,
) -> int:
    """Flag the dive's head/tail labels for a stage-5.1 redraw; the rows
    touched. Canonical and live rows only -- a flag the resolver cannot see
    would re-select the dive every hour -- and, by default, incomplete ones:
    redrawing a frame someone already answered buys nothing."""
    return await _set_flags(
        conn, tenant_id, dive_id, True,
        only_live=True, only_incomplete=only_incomplete, checksums=None,
    )  # fmt: skip


async def clear_headtail_needs_reprocess(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    checksums: Sequence[str] | None = None,
) -> int:
    """Lower the flags of the frames redrawn; the rows touched (0, never an
    error, for a dive with none). Whatever the label's state: a label
    completed or superseded mid-redraw must not keep its flag up. `None` is
    the whole dive (the no-work backstop); `[]` is nothing."""
    return await _set_flags(
        conn, tenant_id, dive_id, False,
        only_live=False, only_incomplete=False, checksums=checksums,
    )  # fmt: skip


# -- predict --------------------------------------------------------------------------


def _needs_prediction(version_param: str) -> str:
    """Capture `c` needs a prediction: a live dot, no human label, and no
    prediction or a stale current one."""
    return f"""{_LIVE_LASER} AND NOT {_LIVE_LABEL}
        AND (
            NOT {_ANY_PREDICTION}
            OR EXISTS (
                SELECT 1 FROM current_head_tail_predictions p
                WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
                  AND (
                      p.predictor_version IS DISTINCT FROM {version_param}
                      OR EXISTS (
                          SELECT 1 FROM laser_labels dead
                          WHERE dead.tenant_id = p.tenant_id
                            AND dead.id = p.laser_label_id AND dead.superseded
                      )
                  )
            )
        )"""


async def next_dive_for_headtail_prediction(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, predictor_version: int
) -> PredictionCandidate | None:
    """The tenant's next dive for the detector: never-predicted work first,
    then the oldest. `predictor_version` is the stage's current version."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at, EXISTS (
                    SELECT 1 FROM captures c
                    WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                      AND c.is_canonical AND {_LIVE_LASER} AND NOT {_LIVE_LABEL}
                      AND NOT {_ANY_PREDICTION}
                ) AS never_predicted
                FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND EXISTS (
                      SELECT 1 FROM captures c
                      WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                        AND c.is_canonical AND {_needs_prediction(":version")}
                  )
                ORDER BY never_predicted DESC, d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id, "version": predictor_version},
        )
    ).one_or_none()
    if row is None:
        return None
    return PredictionCandidate(row.id, row.created_at, row.never_predicted)


async def headtail_predict_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    predictor_version: int,
) -> list[PredictCapture]:
    """The dive's images needing a prediction, with their live dots and what
    their current row is (v1's `select_images_needing_prediction`).

    Stale is a version other than `predictor_version` (NULL included) or a
    `laser_label_id` no longer among the image's live dots. The activity is
    told whether a row exists and whether its dot died, because only it knows
    whether it has a GPU, and so whether it can improve on the row.
    """
    params = {"t": tenant_id, "d": dive_id}
    dots: dict[uuid.UUID, list[LaserDot]] = {}
    for r in await conn.execute(
        text(f"""
            SELECT l.capture_id, l.id, l.x, l.y FROM laser_labels l
            JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
            WHERE c.tenant_id = :t AND c.dive_id = :d AND {VALID_LASER}
            ORDER BY l.number
            """),
        params,
    ):
        dots.setdefault(r.capture_id, []).append(LaserDot(r.id, r.x, r.y))

    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                   {_LIVE_LABEL} AS labelled,
                   p.capture_id IS NOT NULL AS predicted,
                   p.predictor_version, p.laser_label_id
            FROM captures c
            LEFT JOIN current_head_tail_predictions p
              ON p.tenant_id = c.tenant_id AND p.capture_id = c.id
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
            ORDER BY c.number
            """),
        params,
    )
    out = []
    for r in rows:
        live = dots.get(r.id)
        if not live or r.labelled:
            continue
        dead = r.laser_label_id is not None and r.laser_label_id not in {
            d.laser_label_id for d in live
        }
        if r.predicted and r.predictor_version == predictor_version and not dead:
            continue  # fresh
        out.append(
            PredictCapture(
                capture_id=r.id,
                checksum=r.checksum,
                from_v1=r.from_v1,
                dots=tuple(live),
                has_existing_prediction=r.predicted,
                existing_laser_superseded=dead,
            )
        )
    return out


async def persist_headtail_predictions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    rows: Sequence[HeadTailPredictionRow],
) -> int:
    """Append the dive's predictions, abstentions included, all or nothing, in
    the caller's transaction. Returns how many were written.

    Every capture must be the dive's, and a named laser label its capture's:
    the processor runs on infrastructure we don't own, and its output is
    checked before it becomes a row (PLAN.md §9.11).
    """
    rows = list(rows)
    if not rows:
        return 0
    captures = {r.capture_id for r in rows}
    owned = set(
        (
            await conn.execute(
                text("""
                    SELECT id FROM captures
                    WHERE tenant_id = :t AND dive_id = :d AND id = ANY(:ids)
                    """),
                {"t": tenant_id, "d": dive_id, "ids": list(captures)},
            )
        ).scalars()
    )
    if foreign := captures - owned:
        raise ForeignCapture(
            f"not captures of dive {dive_id}: {sorted(map(str, foreign))}"
        )
    named = {r.laser_label_id for r in rows if r.laser_label_id is not None}
    laser_capture = {
        row.id: row.capture_id
        for row in await conn.execute(
            text("""
                SELECT id, capture_id FROM laser_labels
                WHERE tenant_id = :t AND id = ANY(:ids)
                """),
            {"t": tenant_id, "ids": list(named)},
        )
    }
    for r in rows:
        if r.laser_label_id is not None and (
            laser_capture.get(r.laser_label_id) != r.capture_id
        ):
            raise ForeignLaserLabel(
                f"laser label {r.laser_label_id} is not capture {r.capture_id}'s"
            )

    for r in rows:
        await conn.execute(
            text("""
                INSERT INTO head_tail_predictions (
                    tenant_id, capture_id, status, head_x, head_y, tail_x, tail_y,
                    width, height, mask_area_px, silhouette_ratio, crop_x, crop_y,
                    laser_label_id, predictor_version, checkpoint, core_version)
                VALUES (
                    :tenant, :capture_id, :status, :head_x, :head_y, :tail_x,
                    :tail_y, :width, :height, :mask_area_px, :silhouette_ratio,
                    :crop_x, :crop_y, :laser_label_id, :predictor_version,
                    :checkpoint, :core_version)
                """),
            {"tenant": tenant_id, **r.__dict__},
        )
    return len(rows)


# -- populate and the backfill ----------------------------------------------------


async def dives_needing_headtail_population(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[HeadtailCandidate]:
    """Every dive of the tenant needing head/tail tasks (re)populated, oldest
    first: a canonical capture with a valid laser, a prediction (an
    abstention counts), and no completed head/tail label."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.created_at FROM dives d
            WHERE d.tenant_id = :tenant AND d.priority = 'high'
              AND EXISTS (
                  SELECT 1 FROM captures c
                  WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                    AND c.is_canonical AND {_LIVE_LASER} AND {_ANY_PREDICTION}
                    AND NOT EXISTS (
                        SELECT 1 FROM head_tail_labels h
                        WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                          AND h.completed
                    )
              )
            ORDER BY d.created_at, d.id
            """),
        {"tenant": tenant_id},
    )
    return [HeadtailCandidate(r.id, r.created_at) for r in rows]


async def headtail_populate_state(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> PopulateState:
    """What populate and the backfill read of the dive, in one snapshot."""
    params = {"t": tenant_id, "d": dive_id}
    number = (
        await conn.execute(
            text("SELECT number FROM dives WHERE tenant_id = :t AND id = :d"), params
        )
    ).scalar_one_or_none()
    if number is None:
        raise ValueError(f"dive {dive_id} not found")

    candidates = [
        PopulateCandidate(r.id, r.number, r.checksum, r.from_v1, r.captured_at)
        for r in await conn.execute(
            text(f"""
                SELECT c.id, c.number, c.checksum, c.captured_at,
                       c.v1_id IS NOT NULL AS from_v1,
                       (SELECT min(l.number) FROM laser_labels l
                        WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
                          AND {VALID_LASER}) AS first_laser
                FROM captures c
                WHERE c.tenant_id = :t AND c.dive_id = :d
                  AND {_LIVE_LASER} AND NOT {_LIVE_LABEL}
                ORDER BY first_laser, c.number
                """),
            params,
        )
    ]
    predictions = [
        CurrentHeadTailPrediction(
            r.capture_id, r.status, r.head_x, r.head_y, r.tail_x, r.tail_y,
            r.width, r.height, r.silhouette_ratio, r.rejected_low_confidence,
            r.predictor_version,
        )  # fmt: skip
        for r in await conn.execute(
            text("""
                SELECT p.* FROM current_head_tail_predictions p
                JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
                WHERE c.tenant_id = :t AND c.dive_id = :d
                ORDER BY c.number
                """),
            params,
        )
    ]
    labels = [
        LiveHeadTailLabel(r.id, r.capture_id, r.ls_project_id, r.ls_task_id,
                          r.completed)  # fmt: skip
        for r in await conn.execute(
            text("""
                SELECT h.id, h.capture_id, h.ls_project_id, h.ls_task_id, h.completed
                FROM head_tail_labels h
                JOIN captures c ON c.tenant_id = h.tenant_id AND c.id = h.capture_id
                WHERE c.tenant_id = :t AND c.dive_id = :d AND NOT h.superseded
                ORDER BY c.number, h.number
                """),
            params,
        )
    ]
    return PopulateState(number, candidates, predictions, labels)


async def record_head_tail_task(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    *,
    ls_project_id: int,
    ls_task_id: int,
) -> None:
    """Anchor (capture, Label Studio task, project) with a pending label row.

    A new row's source is `human`: the row a labeler will fill. An existing
    row for the (capture, project) is pointed at the task and revived; its
    labeler-owned columns are left to the sync.
    """
    await conn.execute(
        text("""
            INSERT INTO head_tail_labels
                (tenant_id, capture_id, source, ls_project_id, ls_task_id)
            VALUES (:t, :c, 'human', :p, :k)
            ON CONFLICT (tenant_id, capture_id, ls_project_id) DO UPDATE SET
                ls_task_id = excluded.ls_task_id, superseded = false
            """),
        {"t": tenant_id, "c": capture_id, "p": ls_project_id, "k": ls_task_id},
    )


async def supersede_head_tail_labels(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    label_ids: Sequence[uuid.UUID],
) -> int:
    """Dead-letter these of the dive's live, incomplete labels (populate's
    stale rows). Anything else named is left alone. Returns the rows retired."""
    if not label_ids:
        return 0
    result = await conn.execute(
        text("""
            UPDATE head_tail_labels h SET superseded = true
            FROM captures c
            WHERE c.tenant_id = h.tenant_id AND c.id = h.capture_id
              AND h.tenant_id = :t AND c.dive_id = :d AND h.id = ANY(:ids)
              AND NOT h.completed AND NOT h.superseded
            """),
        {"t": tenant_id, "d": dive_id, "ids": list(label_ids)},
    )
    return result.rowcount


class HeadtailCatalog(ServicePrincipal):
    """The head/tail stages' database side, as the orchestrator's service
    principal."""

    async def next_dive_for_headtail_preprocessing(
        self, tenant_id: uuid.UUID
    ) -> HeadtailCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_headtail_preprocessing(conn, tenant_id)

    async def headtail_preprocess_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> HeadtailPreprocessInputs:
        async with self._tenant(tenant_id) as conn:
            return await headtail_preprocess_inputs(conn, tenant_id, dive_id)

    async def set_headtail_needs_reprocess(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, only_incomplete: bool = True
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await set_headtail_needs_reprocess(
                conn, tenant_id, dive_id, only_incomplete=only_incomplete
            )

    async def clear_headtail_needs_reprocess(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        checksums: Sequence[str] | None = None,
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await clear_headtail_needs_reprocess(
                conn, tenant_id, dive_id, checksums
            )

    async def next_dive_for_headtail_prediction(
        self, tenant_id: uuid.UUID, *, predictor_version: int
    ) -> PredictionCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_headtail_prediction(
                conn, tenant_id, predictor_version=predictor_version
            )

    async def headtail_predict_captures(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, predictor_version: int
    ) -> list[PredictCapture]:
        async with self._tenant(tenant_id) as conn:
            return await headtail_predict_captures(
                conn, tenant_id, dive_id, predictor_version=predictor_version
            )

    async def persist_headtail_predictions(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        rows: Sequence[HeadTailPredictionRow],
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_headtail_predictions(conn, tenant_id, dive_id, rows)

    async def dives_needing_headtail_population(
        self, tenant_id: uuid.UUID
    ) -> list[HeadtailCandidate]:
        async with self._tenant(tenant_id) as conn:
            return await dives_needing_headtail_population(conn, tenant_id)

    async def headtail_populate_state(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> PopulateState:
        async with self._tenant(tenant_id) as conn:
            return await headtail_populate_state(conn, tenant_id, dive_id)

    async def record_head_tail_task(
        self,
        tenant_id: uuid.UUID,
        capture_id: uuid.UUID,
        *,
        ls_project_id: int,
        ls_task_id: int,
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await record_head_tail_task(
                conn,
                tenant_id,
                capture_id,
                ls_project_id=ls_project_id,
                ls_task_id=ls_task_id,
            )

    async def supersede_head_tail_labels(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        label_ids: Sequence[uuid.UUID],
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await supersede_head_tail_labels(conn, tenant_id, dive_id, label_ids)
