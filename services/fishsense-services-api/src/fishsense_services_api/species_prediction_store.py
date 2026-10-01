"""The database side of the BioCLIP species pre-annotation stage, tenant-scoped.

**New in v2; v1 has no counterpart** (it had no species model). The classifier
is ported from coral-gardeners-fish-detector@67c8627 (see
`fishsense_services_contracts.species_prediction`); this store is built the
way the head/tail predict stage's is (`headtail_store`), and keeps its rules:

* **predictions are appended** (migration 0034), and every reader judges the
  current one (`current_species_predictions`);
* **the cohort** is a canonical capture of a high-priority dive whose current
  head/tail prediction kept a mask (its `mask_bbox`, 0033) and which has no
  current species prediction at the current version, cropped from that very
  head/tail prediction. So a fallback row (another version) and a row cropped
  from a superseded head/tail prediction are stale, and never-predicted dives
  go first, so the fallback's permanently stale rows can't starve new work;
* the resolver mirrors the selector exactly, or a dive re-fires every hour;
* **the processor's output is checked** (PLAN.md §9.11): a prediction for a
  capture outside the dive, or cropped from another capture's head/tail
  prediction, is refused, and nothing is written.

**Pre-annotation only.** Nothing here writes a species label: a prediction is
shown to a labeler as a suggestion, and a human confirms every label.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "CurrentSpeciesPrediction",
    "ForeignCapture",
    "ForeignHeadtailPrediction",
    "InvalidSpeciesPredictions",
    "LiveSpeciesTask",
    "SpeciesPredictCapture",
    "SpeciesPredictionCandidate",
    "SpeciesPredictionCatalog",
    "SpeciesPredictionRow",
    "SpeciesPredictionState",
    "next_dive_for_species_prediction",
    "persist_species_predictions",
    "species_predict_captures",
    "species_prediction_cohort",
    "species_prediction_state",
]

#: Capture `c`'s current head/tail prediction `p`, with its row `b` for the
#: box (the view predates the column, 0033), when that prediction kept a mask.
_BOXED = """
    JOIN current_head_tail_predictions p
      ON p.tenant_id = c.tenant_id AND p.capture_id = c.id
    JOIN head_tail_predictions b ON b.tenant_id = p.tenant_id AND b.id = p.id
"""


def _fresh(version: str) -> str:
    """Capture `c` has a current species prediction at `version`, cropped
    from its current head/tail prediction `p`."""
    return f"""EXISTS (
        SELECT 1 FROM current_species_predictions s
        WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
          AND s.predictor_version = {version}
          AND s.headtail_prediction_id = p.id
    )"""


#: Capture `c` has any species prediction, abstentions and stale ones included.
_ANY_PREDICTION = """EXISTS (
    SELECT 1 FROM species_predictions s
    WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
)"""


def _work(version: str, *, never_predicted: bool = False) -> str:
    extra = f"AND NOT {_ANY_PREDICTION}" if never_predicted else ""
    return f"""EXISTS (
        SELECT 1 FROM captures c {_BOXED}
        WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
          AND b.mask_bbox IS NOT NULL
          AND NOT {_fresh(version)}
          {extra}
    )"""


def species_prediction_cohort(version: str) -> str:
    """The species-prediction cohort over dive `d`, but for the tenant and
    priority terms the selector adds; `version` is SQL for the stage's current
    version (a bind parameter, or a literal in a view)."""
    return _work(version)


class InvalidSpeciesPredictions(ValueError):
    """The processor's predictions don't fit the dive they were made for."""


class ForeignCapture(InvalidSpeciesPredictions):
    """A prediction for a capture that is not the dive's."""


class ForeignHeadtailPrediction(InvalidSpeciesPredictions):
    """A prediction cropped from a head/tail prediction not its capture's."""


@dataclass(frozen=True)
class SpeciesPredictionCandidate:
    dive_id: uuid.UUID
    created_at: datetime
    #: Some fish has never been classified (not merely a stale row).
    never_predicted: bool


@dataclass(frozen=True)
class SpeciesPredictCapture:
    capture_id: uuid.UUID
    checksum: str
    #: Migrated from v1: its head/tail JPEG may be where v1 wrote it.
    from_v1: bool
    #: The current head/tail prediction, whose kept mask is cropped.
    headtail_prediction_id: uuid.UUID
    mask_bbox: list[int]
    has_existing_prediction: bool


@dataclass(frozen=True)
class SpeciesPredictionRow:
    """One prediction to append (the processor's result, mapped by the
    orchestrator; this package does not import the processing contract)."""

    capture_id: uuid.UUID
    headtail_prediction_id: uuid.UUID
    status: str
    predictor_version: int
    model_id: str
    predicted_choice: str | None = None
    top1_probability: float | None = None
    margin: float | None = None
    #: ``[{"choice": ..., "probability": ...}]``, best first.
    top5: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CurrentSpeciesPrediction:
    id: uuid.UUID
    capture_id: uuid.UUID
    status: str
    predicted_choice: str | None
    top1_probability: float | None
    margin: float | None
    top5: list[dict[str, Any]]
    predictor_version: int
    model_id: str


@dataclass(frozen=True)
class LiveSpeciesTask:
    """A live (not superseded) species label anchored to a Label Studio task."""

    capture_id: uuid.UUID
    ls_project_id: int
    ls_task_id: int
    completed: bool


@dataclass(frozen=True)
class SpeciesPredictionState:
    """One snapshot of a dive, as species populate and the backfill read it."""

    #: v1's dive id for a migrated dive: what `#{n}` in its titles is.
    dive_number: int
    #: The current species prediction of each of the dive's captures.
    predictions: list[CurrentSpeciesPrediction]
    #: The dive's live species labels that have a task, every project.
    tasks: list[LiveSpeciesTask]


async def next_dive_for_species_prediction(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, predictor_version: int
) -> SpeciesPredictionCandidate | None:
    """The tenant's next dive for BioCLIP: never-predicted work first, then
    the oldest. `predictor_version` is the stage's current version."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at,
                       {_work(":version", never_predicted=True)} AS never_predicted
                FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND {species_prediction_cohort(":version")}
                ORDER BY never_predicted DESC, d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id, "version": predictor_version},
        )
    ).one_or_none()
    if row is None:
        return None
    return SpeciesPredictionCandidate(row.id, row.created_at, row.never_predicted)


async def species_predict_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    predictor_version: int,
) -> list[SpeciesPredictCapture]:
    """The dive's fish needing a species prediction, in capture order: the
    cohort's predicate, capture by capture."""
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                   p.id AS headtail_prediction_id, b.mask_bbox,
                   {_ANY_PREDICTION} AS predicted
            FROM captures c {_BOXED}
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              AND b.mask_bbox IS NOT NULL
              AND NOT {_fresh(":version")}
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id, "version": predictor_version},
    )
    return [
        SpeciesPredictCapture(
            capture_id=r.id,
            checksum=r.checksum,
            from_v1=r.from_v1,
            headtail_prediction_id=r.headtail_prediction_id,
            mask_bbox=list(r.mask_bbox),
            has_existing_prediction=r.predicted,
        )
        for r in rows
    ]


async def persist_species_predictions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    rows: Sequence[SpeciesPredictionRow],
) -> int:
    """Append the dive's predictions, abstentions included, all or nothing, in
    the caller's transaction. Returns how many were written.

    Every capture must be the dive's, and every head/tail prediction its
    capture's: the processor runs on infrastructure we don't own, and its
    output is checked before it becomes a row (PLAN.md §9.11). Writes no
    species label.
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
    headtail_capture = {
        row.id: row.capture_id
        for row in await conn.execute(
            text("""
                SELECT id, capture_id FROM head_tail_predictions
                WHERE tenant_id = :t AND id = ANY(:ids)
                """),
            {"t": tenant_id, "ids": list({r.headtail_prediction_id for r in rows})},
        )
    }
    for r in rows:
        if headtail_capture.get(r.headtail_prediction_id) != r.capture_id:
            raise ForeignHeadtailPrediction(
                f"head/tail prediction {r.headtail_prediction_id} is not capture "
                f"{r.capture_id}'s"
            )

    for r in rows:
        await conn.execute(
            text("""
                INSERT INTO species_predictions (
                    tenant_id, capture_id, headtail_prediction_id, status,
                    predictor_version, model_id, predicted_choice,
                    top1_probability, margin, top5)
                VALUES (
                    :tenant, :capture_id, :headtail_prediction_id, :status,
                    :predictor_version, :model_id, :predicted_choice,
                    :top1_probability, :margin, CAST(:top5 AS jsonb))
                """),
            {"tenant": tenant_id, **r.__dict__, "top5": json.dumps(r.top5)},
        )
    return len(rows)


async def species_prediction_state(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> SpeciesPredictionState:
    """What species populate and the backfill read of the dive, in one
    snapshot."""
    params = {"t": tenant_id, "d": dive_id}
    number = (
        await conn.execute(
            text("SELECT number FROM dives WHERE tenant_id = :t AND id = :d"), params
        )
    ).scalar_one_or_none()
    if number is None:
        raise ValueError(f"dive {dive_id} not found")

    predictions = [
        CurrentSpeciesPrediction(
            r.id, r.capture_id, r.status, r.predicted_choice, r.top1_probability,
            r.margin, r.top5, r.predictor_version, r.model_id,
        )  # fmt: skip
        for r in await conn.execute(
            text("""
                SELECT s.* FROM current_species_predictions s
                JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
                WHERE c.tenant_id = :t AND c.dive_id = :d
                ORDER BY c.number
                """),
            params,
        )
    ]
    tasks = [
        LiveSpeciesTask(r.capture_id, r.ls_project_id, r.ls_task_id, r.completed)
        for r in await conn.execute(
            text("""
                SELECT l.capture_id, l.ls_project_id, l.ls_task_id, l.completed
                FROM species_labels l
                JOIN captures c ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
                WHERE c.tenant_id = :t AND c.dive_id = :d AND NOT l.superseded
                  AND l.ls_task_id IS NOT NULL
                ORDER BY c.number, l.number
                """),
            params,
        )
    ]
    return SpeciesPredictionState(number, predictions, tasks)


class SpeciesPredictionCatalog(ServicePrincipal):
    """The species prediction stage's database side, as the orchestrator's
    service principal."""

    async def next_dive_for_species_prediction(
        self, tenant_id: uuid.UUID, *, predictor_version: int
    ) -> SpeciesPredictionCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_species_prediction(
                conn, tenant_id, predictor_version=predictor_version
            )

    async def species_predict_captures(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, predictor_version: int
    ) -> list[SpeciesPredictCapture]:
        async with self._tenant(tenant_id) as conn:
            return await species_predict_captures(
                conn, tenant_id, dive_id, predictor_version=predictor_version
            )

    async def persist_species_predictions(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        rows: Sequence[SpeciesPredictionRow],
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_species_predictions(conn, tenant_id, dive_id, rows)

    async def species_prediction_state(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPredictionState:
        async with self._tenant(tenant_id) as conn:
            return await species_prediction_state(conn, tenant_id, dive_id)
