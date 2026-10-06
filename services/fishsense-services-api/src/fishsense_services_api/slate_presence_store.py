"""The database side of the slate detector, tenant-scoped.

**New in v2.** v1's slate predictor estimated a slate's pose and was retired
(2026-08-03); this one answers presence only: is a dive slate anywhere in the
frame? The model is 2026-10-03_slate_detector@95a77d95's (see
`fishsense_services_contracts.slate_presence`). Built the way the species
prediction store is (`species_prediction_store`), with its rules:

* **predictions are appended** (migration slate_01), and every reader judges
  the current one (`current_slate_presence`);
* **the cohort** is a dive of **any priority** whose device has a current
  pinhole calibration (the frame is rectified; `camera_sql`) and which has a
  canonical capture with no current prediction at the current model version.
  Any priority because this is for dives nobody labelled; every other cohort
  is high-only, deliberately. An abstention (`decode_failed`) is a
  prediction, so a raw that never decodes doesn't re-select its dive hourly.
  Oldest first;
* the resolver mirrors the selector exactly, or a dive re-fires every hour;
* **the processor's output is checked** (PLAN.md §9.11): a prediction for a
  capture outside the dive is refused, and nothing is written.

**What the automatic chain reads** (`slate_frames`, `slate_presence`): the
current prediction of each canonical capture, whatever its version -- until a
re-prediction lands it is the best there is. `slate_frames` is the dive's
slate frames at the operating point (`SLATE_PRESENCE_THRESHOLD`, the
contracts' value; this package does not import the contracts, and a test in
the orchestrator pins the two equal), to exclude from fish measurement and to
use as calibration candidates.

The other reader is stage 9 (`slate_store`): a dive with no slate labels has
its slate frames queued in its slate project.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.camera_sql import RECTIFIABLE_CAMERA_MODEL, RECTIFIABLE_DIVE
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "SLATE_PRESENCE_THRESHOLD",
    "CurrentSlatePresence",
    "ForeignCapture",
    "InvalidSlatePresence",
    "SlateDetectionCandidate",
    "SlateDetectionCapture",
    "SlateDetectionInputs",
    "SlateDetectionUnavailable",
    "SlatePresenceCatalog",
    "SlatePresenceRow",
    "detector_slate_frame",
    "next_dive_for_slate_detection",
    "persist_slate_presence",
    "slate_detection_cohort",
    "slate_detection_inputs",
    "slate_frames",
    "slate_presence",
]

#: P(slate) at or above this is a slate frame: the contracts'
#: `SLATE_PRESENCE_THRESHOLD`, the operating point the model's CV numbers were
#: measured at (precision 0.999, recall 0.993).
SLATE_PRESENCE_THRESHOLD = 0.5


def detector_slate_frame(threshold: float = SLATE_PRESENCE_THRESHOLD) -> str:
    """Capture `c`'s current prediction says slate. A literal threshold, so
    the predicate can be spelled into a view."""
    return f"""EXISTS (
        SELECT 1 FROM current_slate_presence sp_p
        WHERE sp_p.tenant_id = c.tenant_id AND sp_p.capture_id = c.id
          AND sp_p.probability >= {float(threshold)!r}
    )"""


def _fresh(version: str) -> str:
    """Capture `c` has a current prediction at `version`, abstentions
    included."""
    return f"""EXISTS (
        SELECT 1 FROM current_slate_presence p
        WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id
          AND p.model_version = {version}
    )"""


def slate_detection_cohort(version: str) -> str:
    """The slate-detection cohort over dive `d`, but for the tenant term the
    selector adds (there is no priority term); `version` is SQL for the
    model's current version (a bind parameter, or a literal in a view)."""
    return f"""{RECTIFIABLE_DIVE} AND EXISTS (
        SELECT 1 FROM captures c
        WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
          AND NOT {_fresh(version)}
    )"""


class SlateDetectionUnavailable(ValueError):
    """A dive the detector cannot be resolved for: the reason is in the
    message."""


class InvalidSlatePresence(ValueError):
    """The processor's predictions don't fit the dive they were made for."""


class ForeignCapture(InvalidSlatePresence):
    """A prediction for a capture that is not the dive's."""


@dataclass(frozen=True)
class SlateDetectionCandidate:
    dive_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class SlateDetectionCapture:
    capture_id: uuid.UUID
    #: What its staged raw is keyed by.
    checksum: str


@dataclass(frozen=True)
class SlateDetectionInputs:
    dive_id: uuid.UUID
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    captures: list[SlateDetectionCapture]


@dataclass(frozen=True)
class SlatePresenceRow:
    """One prediction to append (the processor's result, mapped by the
    orchestrator; this package does not import the processing contract)."""

    capture_id: uuid.UUID
    status: str
    probability: float | None
    model_version: int
    weights_sha256: str


@dataclass(frozen=True)
class CurrentSlatePresence:
    id: uuid.UUID
    capture_id: uuid.UUID
    status: str
    #: P(slate); None on an abstention.
    probability: float | None
    model_version: int
    weights_sha256: str
    created_at: datetime


async def next_dive_for_slate_detection(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, model_version: int
) -> SlateDetectionCandidate | None:
    """The tenant's oldest dive with a canonical capture the current model
    has not predicted, whatever its priority."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM dives d
                WHERE d.tenant_id = :tenant
                  AND {slate_detection_cohort(":version")}
                ORDER BY d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id, "version": model_version},
        )
    ).one_or_none()
    return None if row is None else SlateDetectionCandidate(row.id, row.created_at)


async def slate_detection_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    model_version: int,
) -> SlateDetectionInputs:
    """The dive's intrinsics (its device's current pinhole calibration) and
    its canonical captures the current model has not predicted, in capture
    order: the cohort's predicate, capture by capture."""
    camera = (
        await conn.execute(
            text("""
                SELECT cc.camera_matrix, cc.distortion_coefficients
                FROM dives d
                JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                WHERE d.tenant_id = :t AND d.id = :d AND cc.camera_model = :model
                """),
            {"t": tenant_id, "d": dive_id, "model": RECTIFIABLE_CAMERA_MODEL},
        )
    ).one_or_none()
    if camera is None:
        raise SlateDetectionUnavailable(
            f"dive_id={dive_id} has no pinhole camera calibration for its device"
        )
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum FROM captures c
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              AND NOT {_fresh(":version")}
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id, "version": model_version},
    )
    return SlateDetectionInputs(
        dive_id=dive_id,
        camera_matrix=camera.camera_matrix,
        distortion_coefficients=list(camera.distortion_coefficients),
        captures=[SlateDetectionCapture(r.id, r.checksum) for r in rows],
    )


async def persist_slate_presence(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    rows: Sequence[SlatePresenceRow],
) -> int:
    """Append the dive's predictions, abstentions included, all or nothing, in
    the caller's transaction. Returns how many were written.

    Every capture must be the dive's: the processor runs on infrastructure we
    don't own, and its output is checked before it becomes a row (PLAN.md
    §9.11)."""
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
    for r in rows:
        await conn.execute(
            text("""
                INSERT INTO slate_presence_predictions (
                    tenant_id, capture_id, status, probability, model_version,
                    weights_sha256)
                VALUES (:tenant, :capture_id, :status, :probability,
                        :model_version, :weights_sha256)
                """),
            {"tenant": tenant_id, **r.__dict__},
        )
    return len(rows)


async def slate_presence(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[CurrentSlatePresence]:
    """The current prediction of each of the dive's canonical captures that
    has one, in capture order."""
    rows = await conn.execute(
        text("""
            SELECT p.id, p.capture_id, p.status, p.probability, p.model_version,
                   p.weights_sha256, p.created_at
            FROM current_slate_presence p
            JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id},
    )
    return [
        CurrentSlatePresence(
            r.id, r.capture_id, r.status, r.probability, r.model_version,
            r.weights_sha256, r.created_at,
        )  # fmt: skip
        for r in rows
    ]


async def slate_frames(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    threshold: float = SLATE_PRESENCE_THRESHOLD,
) -> list[tuple[uuid.UUID, float]]:
    """The dive's slate frames: each canonical capture whose current
    prediction is at or above `threshold`, with its probability, in capture
    order. For the automatic chain: exclude them from fish measurement, use
    them as calibration candidates."""
    rows = await conn.execute(
        text("""
            SELECT p.capture_id, p.probability
            FROM current_slate_presence p
            JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              AND p.probability >= :threshold
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id, "threshold": threshold},
    )
    return [(r.capture_id, r.probability) for r in rows]


class SlatePresenceCatalog(ServicePrincipal):
    """The slate detector's database side, as the orchestrator's service
    principal (and the automatic chain's)."""

    async def next_dive_for_slate_detection(
        self, tenant_id: uuid.UUID, *, model_version: int
    ) -> SlateDetectionCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_slate_detection(
                conn, tenant_id, model_version=model_version
            )

    async def slate_detection_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, model_version: int
    ) -> SlateDetectionInputs:
        async with self._tenant(tenant_id) as conn:
            return await slate_detection_inputs(
                conn, tenant_id, dive_id, model_version=model_version
            )

    async def persist_slate_presence(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        rows: Sequence[SlatePresenceRow],
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_slate_presence(conn, tenant_id, dive_id, rows)

    async def slate_presence(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[CurrentSlatePresence]:
        async with self._tenant(tenant_id) as conn:
            return await slate_presence(conn, tenant_id, dive_id)

    async def slate_frames(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        *,
        threshold: float = SLATE_PRESENCE_THRESHOLD,
    ) -> list[tuple[uuid.UUID, float]]:
        async with self._tenant(tenant_id) as conn:
            return await slate_frames(conn, tenant_id, dive_id, threshold=threshold)
