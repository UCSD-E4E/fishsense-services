"""The database side of the automatic-results track, tenant-scoped.

New in v2 (no v1 counterpart): fish lengths with no human label, by the chain
cscw-fishsense2027@96a8da07 validated (PAPER.md §6; e2e_measurement/
run_e2e.py, score.py, tail/evaluate.py). Decided 2026-10-06:

* **a separate track** (migration 0036): its own append-only tables,
  producer ``automatic``. Nothing here reads a label as an input or writes a
  label, a human-path prediction or a `measurements` row; the human path is
  exactly as it was;
* **the backlog cohort** (`AUTOMATIC_RESULTS_COHORT`): a dive of **any**
  priority with no human measurement (no `measurements` row on any of its
  captures), renderable (`camera_sql.RECTIFIABLE_DIVE`), with automatic work
  outstanding; the oldest first, so the orchestrator can take the oldest
  across the tenants it serves. Work is: a canonical frame with no automatic
  head/tail at the current version; a kept mask with no automatic species at
  the current version; no automatic calibration at the current version, or one
  older than the dive's newest automatic head/tail (its candidates may have
  changed); a predicted fish with no current automatic length while a
  calibration exists. Every one of those drains: abstentions, refused
  calibrations and refused lengths are rows;
* **slate frames** come from the slate-presence detector
  (`slate_presence_store.slate_frames`, wired in the orchestrator's stage),
  through `SlateFrames` -- ``(conn, tenant, dive) -> [(capture_id, p)]``.
  At ``p >= 0.5`` (`SLATE_FRAME_THRESHOLD`) a frame is not measured as a fish
  and is a candidate for the label-free calibration (the slate is the rigid
  object the paper validated it on, §4.3);
* **the processor's output is checked** (PLAN.md §9.11): a row for a capture
  outside the dive, or naming another capture's automatic head/tail, is
  refused and nothing is written.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.camera_sql import RECTIFIABLE_DIVE
from fishsense_services_api.service_principal import ServicePrincipal
from fishsense_services_api.slate_presence_store import detector_slate_frame

__all__ = [
    "AUTOMATIC_PRODUCER",
    "AUTOMATIC_RESULTS_COHORT",
    "AUTOMATIC_FRAME_MAX_FAILURES",
    "AUTOMATIC_RESULTS_MAX_FAILURES",
    "AUTOMATIC_RESULTS_STEPS",
    "automatic_results_cohort",
    "record_automatic_frame_failures",
    "record_automatic_results_failure",
    "SLATE_FRAME_THRESHOLD",
    "AutomaticCalibrationCandidate",
    "AutomaticCalibrationInputs",
    "AutomaticCalibrationRow",
    "AutomaticCandidate",
    "AutomaticFrame",
    "AutomaticFramesInputs",
    "AutomaticHeadTailRow",
    "AutomaticMeasureCapture",
    "AutomaticMeasureInputs",
    "AutomaticMeasurementRow",
    "AutomaticResultsCatalog",
    "AutomaticSpeciesCapture",
    "AutomaticSpeciesRow",
    "ForeignCapture",
    "ForeignHeadTail",
    "InvalidAutomaticResults",
    "MeasurementCalibration",
    "SlateFrames",
    "automatic_calibration_inputs",
    "automatic_frames_inputs",
    "automatic_measure_inputs",
    "automatic_species_captures",
    "next_dive_for_automatic_results",
    "no_slate_frames",
    "persist_automatic_calibration",
    "persist_automatic_head_tails",
    "persist_automatic_measurements",
    "persist_automatic_species",
]

AUTOMATIC_PRODUCER = "automatic"

#: A frame the slate-presence detector scores at or above this is a slate
#: frame: excluded from fish measurement, a calibration candidate.
SLATE_FRAME_THRESHOLD = 0.5

#: ``(conn, tenant_id, dive_id) -> [(capture_id, probability)]``: the
#: slate-presence detector's store function, once it lands.
SlateFrames = Callable[
    [AsyncConnection, uuid.UUID, uuid.UUID], Awaitable[list[tuple[uuid.UUID, float]]]
]


async def no_slate_frames(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[tuple[uuid.UUID, float]]:
    """No frame is known to show a slate: the catalog's default, for tests
    and tools that don't read the detector. Production passes the detector's
    `slate_frames` (orchestrator `automatic_results.stage`)."""
    del conn, tenant_id, dive_id
    return []


async def _slate_probabilities(
    slate_frames: SlateFrames,
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
) -> dict[uuid.UUID, float]:
    return {c: float(p) for c, p in await slate_frames(conn, tenant_id, dive_id)}


class InvalidAutomaticResults(ValueError):
    """The processor's output doesn't fit the dive it was made for."""


class ForeignCapture(InvalidAutomaticResults):
    """A row for a capture that is not the dive's."""


class ForeignHeadTail(InvalidAutomaticResults):
    """A row naming an automatic head/tail that is not its capture's."""


# -- the cohort -------------------------------------------------------------------


#: The capture's latest automatic head/tail, `h`, at the version `:hv`.
def _latest(table: str, capture: str = "c") -> str:
    """Capture `capture`'s latest row of `table`, as its `current_*` view
    picks it, but as one index lookup on (tenant_id, capture_id, seq). Read
    through the view, correlated per frame or per dive, Postgres rebuilt it --
    sorting the whole table -- every time: the shape that took the slate
    cohort past the app role's 30 s timeout once its backlog drained
    (2026-10-14)."""
    return f"""(
        SELECT latest.* FROM {table} latest
        WHERE latest.tenant_id = {capture}.tenant_id
          AND latest.capture_id = {capture}.id
        ORDER BY latest.seq DESC
        LIMIT 1
    )"""


_FRAME_DONE = f"""EXISTS (
    SELECT 1 FROM {_latest("automatic_head_tail_predictions")} h
    WHERE h.predictor_version = :hv
)"""

#: Capture `c` is one the slate-presence detector currently calls a slate
#: (0035). The same rule as `slate_frames`, in SQL, so the cohort and the
#: inputs agree.
_DETECTOR_SLATE = detector_slate_frame(SLATE_FRAME_THRESHOLD)


def _calibration_session(tenant: str, dive: str) -> str:
    """The dive is another dive's calibration source (decided 2026-10-15): its
    slate frames feed the label-free fit its borrowers use, and nothing in it
    is segmented, named or measured as a fish."""
    return f"""EXISTS (
    SELECT 1 FROM dives borrower
    WHERE borrower.tenant_id = {tenant}
      AND borrower.calibration_source_dive_id = {dive}
      AND borrower.id <> {dive}
)"""


_SESSION = _calibration_session("d.tenant_id", "d.id")

#: An operator listed the dive as one to leave alone (0039).
_EXCLUDED = """EXISTS (
    SELECT 1 FROM automatic_results_exclusions x
    WHERE x.tenant_id = d.tenant_id AND x.dive_id = d.id
)"""

#: A dive's failed runs before the cohort leaves it for an operator (0040).
AUTOMATIC_RESULTS_MAX_FAILURES = 3
#: Runs a frame may be left out of (0044) before it is a terminal
#: ``frame_failed`` abstention. Separate runs, each after the processor's own
#: tries: a transient fault rarely hits one frame three runs running.
AUTOMATIC_FRAME_MAX_FAILURES = 3

#: The steps a failure is recorded against (0040's check).
AUTOMATIC_RESULTS_STEPS = ("frames", "calibration", "lengths", "species")


#: The dive failed as often as the cohort allows (0040).
def _dive_rows_newest(table: str) -> str:
    """When the newest `table` row on dive `d`'s captures was written: each
    capture's newest row by its `(tenant_id, capture_id, seq)` index, never a
    join over the table (the cohort's plan test pins it)."""
    return f"""(SELECT max(x.created_at) FROM captures xc
            CROSS JOIN LATERAL (
                SELECT latest.created_at FROM {table} latest
                WHERE latest.tenant_id = xc.tenant_id AND latest.capture_id = xc.id
                ORDER BY latest.seq DESC LIMIT 1
            ) x
            WHERE xc.tenant_id = d.tenant_id AND xc.dive_id = d.id)"""


#: When dive `d` last got further: its newest automatic row of any kind.
_LAST_PROGRESS = f"""greatest(
        {_dive_rows_newest("automatic_head_tail_predictions")},
        {_dive_rows_newest("automatic_species_predictions")},
        {_dive_rows_newest("automatic_measurements")},
        (SELECT max(lc.created_at) FROM automatic_laser_calibrations lc
         WHERE lc.tenant_id = d.tenant_id AND lc.dive_id = d.id))"""


def _strikes(since: str = "NULL") -> str:
    return f"""(
        SELECT count(*) FROM automatic_results_failures f
        WHERE f.tenant_id = d.tenant_id AND f.dive_id = d.id
          AND f.created_at > coalesce({since}, '-infinity'::timestamptz)
    )"""


#: Dive `d` failed `AUTOMATIC_RESULTS_MAX_FAILURES` runs with nothing of it
#: written since the first of them. Strikes before the dive last got further
#: don't count (review of #85): three transient faults weeks apart, each run
#: between getting further, are not a bad dive. Progress is only looked up
#: for a dive with that many strikes at all (CASE evaluates in order), so
#: the hourly cohort stays one index probe per dive for the rest.
_FAILED_OUT = f"""CASE
    WHEN {_strikes()} < {AUTOMATIC_RESULTS_MAX_FAILURES} THEN false
    ELSE {_strikes(_LAST_PROGRESS)} >= {AUTOMATIC_RESULTS_MAX_FAILURES}
END"""

#: Dive `d` has a human measurement: any `measurements` row on its captures.
_HUMAN_MEASURED = """EXISTS (
    SELECT 1 FROM measurements m
    JOIN captures mc ON mc.tenant_id = m.tenant_id AND mc.id = m.capture_id
    WHERE mc.tenant_id = d.tenant_id AND mc.dive_id = d.id
)"""

#: A canonical frame with no automatic head/tail at the current version; in
#: a calibration session, only its slate frames.
_FRAMES_WORK = f"""EXISTS (
        SELECT 1 FROM captures c
        WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
          AND NOT {_FRAME_DONE}
          AND (NOT {_SESSION} OR {_DETECTOR_SLATE})
    )"""

#: A kept mask with no automatic species at the current version (never in a
#: calibration session).
_SPECIES_WORK = f"""NOT {_SESSION} AND EXISTS (
        SELECT 1 FROM captures c
        CROSS JOIN LATERAL {_latest("automatic_head_tail_predictions")} h
        WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
          AND h.status = 'predicted' AND NOT {_DETECTOR_SLATE}
          AND NOT EXISTS (
              SELECT 1 FROM {_latest("automatic_species_predictions")} s
              WHERE s.automatic_head_tail_prediction_id = h.id
                AND s.predictor_version = :sv
          )
    )"""

#: No current automatic calibration at the current version, or one older than
#: the dive's newest frame or slate score.
_CALIBRATION_WORK = """(
        EXISTS (
            SELECT 1 FROM captures c
            WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
        )
        AND NOT EXISTS (
            SELECT 1 FROM current_automatic_laser_calibrations a
            WHERE a.tenant_id = d.tenant_id AND a.dive_id = d.id
              AND a.algorithm_version = :cv
              -- Stale against new frames, and against a slate score written
              -- after it: the detector's slate frames are the candidates. A
              -- capture's newest row is its current one (append-only), so
              -- "any row newer" is "the newest is newer".
              AND NOT EXISTS (
                  SELECT 1 FROM captures c
                  WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                    AND (
                      EXISTS (
                          SELECT 1 FROM automatic_head_tail_predictions h
                          WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                            AND h.created_at > a.created_at
                      )
                      OR EXISTS (
                          SELECT 1 FROM slate_presence_predictions sp
                          WHERE sp.tenant_id = c.tenant_id AND sp.capture_id = c.id
                            AND sp.created_at > a.created_at
                      )
                    )
              )
        )
    )"""

#: A predicted fish with no current automatic length while a calibration
#: exists (never in a calibration session).
_LENGTHS_WORK = f"""NOT {_SESSION} AND EXISTS (
        SELECT 1 FROM automatic_measurement_calibrations mc
        JOIN captures c ON c.tenant_id = mc.tenant_id AND c.dive_id = mc.dive_id
        CROSS JOIN LATERAL {_latest("automatic_head_tail_predictions")} h
        WHERE mc.tenant_id = d.tenant_id AND mc.dive_id = d.id AND c.is_canonical
          AND h.status = 'predicted'
          -- A frame the detector now calls a slate is never measured
          -- (automatic_measure_inputs), so it is no length outstanding.
          AND NOT {_DETECTOR_SLATE}
          -- current_automatic_measurements: the latest length whose head/tail
          -- and calibration are still the frame's and the dive's.
          AND (
              SELECT m.algorithm_version FROM automatic_measurements m
              WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                AND m.automatic_head_tail_prediction_id = h.id
                AND m.calibration_source = mc.calibration_source
                AND m.automatic_laser_calibration_id
                    IS NOT DISTINCT FROM mc.automatic_laser_calibration_id
                AND m.laser_calibration_id IS NOT DISTINCT FROM mc.laser_calibration_id
              ORDER BY m.seq DESC
              LIMIT 1
          ) IS DISTINCT FROM :mv
    )"""


def automatic_results_cohort(*, species: bool = True) -> str:
    """The backlog cohort over dive `d`, but for the tenant term the selector
    adds. Any priority. `:hv`, `:sv`, `:cv`, `:mv` are the stages' current
    versions (`:sv` unused with `species` off). Species has its own switch
    (2026-10-15): off, a dive whose only work is species is not in it."""
    work = [_FRAMES_WORK, _CALIBRATION_WORK, _LENGTHS_WORK]
    if species:
        work.insert(1, _SPECIES_WORK)
    return f"""{RECTIFIABLE_DIVE}
    AND NOT {_HUMAN_MEASURED}
    AND NOT {_EXCLUDED}
    AND NOT {_FAILED_OUT}
    AND ({" OR ".join(f"({w})" for w in work)})"""


#: The cohort with species on, the default.
AUTOMATIC_RESULTS_COHORT = automatic_results_cohort()


@dataclass(frozen=True)
class AutomaticCandidate:
    dive_id: uuid.UUID
    created_at: datetime


async def next_dive_for_automatic_results(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    headtail_version: int,
    species_version: int,
    calibration_version: str,
    measurement_version: str,
    species: bool = True,
    exclude: Sequence[uuid.UUID] = (),
) -> AutomaticCandidate | None:
    """The tenant's oldest dive in the backlog cohort, other than `exclude`
    (the dives the run already took)."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM dives d
                WHERE d.tenant_id = :t AND {automatic_results_cohort(species=species)}
                  AND NOT d.id = ANY(CAST(:exclude AS uuid[]))
                ORDER BY d.created_at, d.id
                LIMIT 1
                """),
            {"t": tenant_id, "hv": headtail_version, "sv": species_version,
             "cv": calibration_version, "mv": measurement_version,
             "exclude": list(exclude)},
        )
    ).one_or_none()  # fmt: skip
    return None if row is None else AutomaticCandidate(row.id, row.created_at)


async def record_automatic_results_failure(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    step: str,
    error: str,
) -> None:
    """A failed run of the dive's `step` (0040); at
    `AUTOMATIC_RESULTS_MAX_FAILURES` with nothing of the dive written since
    (`_FAILED_OUT`), the cohort leaves the dive."""
    await conn.execute(
        text(
            "INSERT INTO automatic_results_failures "
            "(tenant_id, dive_id, step, error) VALUES (:t, :d, :step, :error)"
        ),
        {"t": tenant_id, "d": dive_id, "step": step, "error": error[:4000]},
    )


async def record_automatic_frame_failures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    failures: Sequence[tuple[uuid.UUID, str]],
    *,
    headtail_version: int,
) -> list[uuid.UUID]:
    """Frames the processor left out of a run (0044). Never one of the
    dive's strikes: the frame stays owed, so the next run asks for it again.
    A frame left out at `AUTOMATIC_FRAME_MAX_FAILURES` runs of this version
    gets its terminal ``frame_failed`` head/tail here, in the same
    transaction, so it drains and the dive goes on. Returns those frames."""
    failures = list(failures)
    if not failures:
        return []
    await _check_captures(conn, tenant_id, dive_id, [c for c, _ in failures])
    for capture_id, error in failures:
        await conn.execute(
            text(
                "INSERT INTO automatic_frame_failures "
                "(tenant_id, capture_id, predictor_version, error) "
                "VALUES (:t, :c, :hv, :error)"
            ),
            {"t": tenant_id, "c": capture_id, "hv": headtail_version,
             "error": error[:4000]},
        )  # fmt: skip
    exhausted = list(
        (
            await conn.execute(
                text("""
                    SELECT f.capture_id FROM automatic_frame_failures f
                    WHERE f.tenant_id = :t AND f.capture_id = ANY(:ids)
                      AND f.predictor_version = :hv
                    GROUP BY f.capture_id
                    HAVING count(*) >= :max
                    """),
                {"t": tenant_id, "ids": [c for c, _ in failures],
                 "hv": headtail_version, "max": AUTOMATIC_FRAME_MAX_FAILURES},
            )
        ).scalars()
    )  # fmt: skip
    await _insert(
        conn,
        "automatic_head_tail_predictions",
        tenant_id,
        [
            AutomaticHeadTailRow(
                capture_id=c, status="frame_failed", predictor_version=headtail_version
            )
            for c in exhausted
        ],
    )
    return exhausted


# -- frames: the GPU stage's input --------------------------------------------------


@dataclass(frozen=True)
class AutomaticFrame:
    capture_id: uuid.UUID
    checksum: str
    #: Migrated from v1: its JPEG may be where v1 wrote it.
    from_v1: bool
    #: The slate-presence detector's score; None with no detector.
    slate_probability: float | None = None

    @property
    def is_slate(self) -> bool:
        return (
            self.slate_probability is not None
            and self.slate_probability >= SLATE_FRAME_THRESHOLD
        )


@dataclass(frozen=True)
class AutomaticFramesInputs:
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    frames: list[AutomaticFrame]


def _flat(values) -> list[float]:
    if values and all(isinstance(v, list) for v in values):
        return [float(x) for row in values for x in row]
    return [float(x) for x in values]


async def _pinhole_camera(conn: AsyncConnection, tenant_id, dive_id):
    row = (
        await conn.execute(
            text("""
                SELECT cc.id, cc.camera_matrix, cc.distortion_coefficients
                FROM dives d
                JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                WHERE d.tenant_id = :t AND d.id = :d AND cc.camera_model = 'pinhole'
                """),
            {"t": tenant_id, "d": dive_id},
        )
    ).one_or_none()
    if row is None:
        raise ValueError(f"dive {dive_id} has no pinhole camera calibration")
    return row


async def automatic_frames_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    headtail_version: int,
    slate_frames: SlateFrames = no_slate_frames,
) -> AutomaticFramesInputs:
    """The dive's canonical frames with no automatic head/tail at the current
    version, in capture order, each with its slate score; and the intrinsics
    the raw is rectified with."""
    camera = await _pinhole_camera(conn, tenant_id, dive_id)
    slate = await _slate_probabilities(slate_frames, conn, tenant_id, dive_id)
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1
            FROM captures c
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              AND NOT {_FRAME_DONE}
              AND (NOT {_calibration_session(":t", ":d")} OR {_DETECTOR_SLATE})
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id, "hv": headtail_version},
    )
    return AutomaticFramesInputs(
        camera_matrix=[[float(x) for x in r] for r in camera.camera_matrix],
        distortion_coefficients=_flat(camera.distortion_coefficients),
        frames=[
            AutomaticFrame(r.id, r.checksum, r.from_v1, slate.get(r.id)) for r in rows
        ],
    )


@dataclass(frozen=True)
class AutomaticHeadTailRow:
    """One frame's automatic dot and head/tail (or abstention) to append."""

    capture_id: uuid.UUID
    status: str
    predictor_version: int
    laser_x: float | None = None
    laser_y: float | None = None
    laser_confidence: float | None = None
    laser_predictor_version: int | None = None
    laser_checkpoint: str | None = None
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
    mask_bbox: list[int] | None = None
    sam_score: float | None = None
    slate_probability: float | None = None
    checkpoint: str | None = None
    core_version: str | None = None


async def _check_captures(conn, tenant_id, dive_id, captures) -> None:
    captures = set(captures)
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


async def _check_head_tails(conn, tenant_id, rows) -> None:
    named = {r.automatic_head_tail_prediction_id for r in rows}
    owner = {
        r.id: r.capture_id
        for r in await conn.execute(
            text("""
                SELECT id, capture_id FROM automatic_head_tail_predictions
                WHERE tenant_id = :t AND id = ANY(:ids)
                """),
            {"t": tenant_id, "ids": list(named)},
        )
    }
    for r in rows:
        if owner.get(r.automatic_head_tail_prediction_id) != r.capture_id:
            raise ForeignHeadTail(
                f"automatic head/tail {r.automatic_head_tail_prediction_id} is not "
                f"capture {r.capture_id}'s"
            )


async def _insert(conn, table: str, tenant_id, rows: Sequence) -> list[uuid.UUID]:
    ids = []
    for r in rows:
        values = asdict(r)
        columns = ", ".join(["tenant_id", *values])
        params = ", ".join([":tenant_id", *(f":{k}" for k in values)])
        ids.append(
            (
                await conn.execute(
                    text(f"INSERT INTO {table} ({columns}) VALUES ({params}) "
                         "RETURNING id"),
                    {"tenant_id": tenant_id, **values},
                )
            ).scalar_one()
        )  # fmt: skip
    return ids


async def persist_automatic_head_tails(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    rows: Sequence[AutomaticHeadTailRow],
) -> int:
    """Append the dive's automatic head/tails, abstentions included, all or
    nothing, in the caller's transaction. Returns how many were written."""
    rows = list(rows)
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, dive_id, [r.capture_id for r in rows])
    return len(await _insert(conn, "automatic_head_tail_predictions", tenant_id, rows))


# -- species ------------------------------------------------------------------------


@dataclass(frozen=True)
class AutomaticSpeciesCapture:
    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    automatic_head_tail_prediction_id: uuid.UUID
    mask_bbox: list[int]
    has_existing_prediction: bool


async def automatic_species_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    species_version: int,
) -> list[AutomaticSpeciesCapture]:
    """The dive's kept automatic masks with no automatic species at the
    current version cropped from them, in capture order."""
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                   h.id AS head_tail_id, h.mask_bbox,
                   s.id IS NOT NULL AS has_existing
            FROM captures c
            JOIN current_automatic_head_tail_predictions h
              ON h.tenant_id = c.tenant_id AND h.capture_id = c.id
            LEFT JOIN current_automatic_species_predictions s
              ON s.tenant_id = c.tenant_id AND s.capture_id = c.id
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              AND h.status = 'predicted' AND NOT {_DETECTOR_SLATE}
              AND NOT {_calibration_session(":t", ":d")}
              AND (s.id IS NULL OR s.automatic_head_tail_prediction_id <> h.id
                   OR s.predictor_version IS DISTINCT FROM :sv)
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id, "sv": species_version},
    )
    return [
        AutomaticSpeciesCapture(r.id, r.checksum, r.from_v1, r.head_tail_id,
                                list(r.mask_bbox), r.has_existing)  # fmt: skip
        for r in rows
    ]


@dataclass(frozen=True)
class AutomaticSpeciesRow:
    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    status: str
    predictor_version: int
    model_id: str
    predicted_choice: str | None = None
    top1_probability: float | None = None
    margin: float | None = None
    top5: list[dict] = field(default_factory=list)


async def persist_automatic_species(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    rows: Sequence[AutomaticSpeciesRow],
) -> int:
    """Append zero-shot species for the dive's automatic masks; each must name
    its own capture's automatic head/tail."""
    rows = list(rows)
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, dive_id, [r.capture_id for r in rows])
    await _check_head_tails(conn, tenant_id, rows)
    for r in rows:
        await conn.execute(
            text("""
                INSERT INTO automatic_species_predictions (
                    tenant_id, capture_id, automatic_head_tail_prediction_id, status,
                    predictor_version, model_id, predicted_choice, top1_probability,
                    margin, top5)
                VALUES (:t, :capture_id, :automatic_head_tail_prediction_id, :status,
                        :predictor_version, :model_id, :predicted_choice,
                        :top1_probability, :margin, CAST(:top5 AS jsonb))
                """),
            {"t": tenant_id, **asdict(r), "top5": json.dumps(r.top5)},
        )
    return len(rows)


# -- calibration ----------------------------------------------------------------------


@dataclass(frozen=True)
class AutomaticCalibrationCandidate:
    """A slate frame with its automatic dot."""

    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    x: float
    y: float


@dataclass(frozen=True)
class AutomaticCalibrationInputs:
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    candidates: list[AutomaticCalibrationCandidate]
    #: Every automatic dot of the dive: the dive line is fitted through them.
    line_dots: list[list[float]]


async def automatic_calibration_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    slate_frames: SlateFrames = no_slate_frames,
) -> AutomaticCalibrationInputs:
    """What the label-free fit reads: the dive's slate frames (p >= 0.5) with
    a current automatic dot, in capture order, and every automatic dot."""
    camera = await _pinhole_camera(conn, tenant_id, dive_id)
    slate = await _slate_probabilities(slate_frames, conn, tenant_id, dive_id)
    rows = (
        await conn.execute(
            text("""
                SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                       h.laser_x, h.laser_y
                FROM captures c
                JOIN current_automatic_head_tail_predictions h
                  ON h.tenant_id = c.tenant_id AND h.capture_id = c.id
                WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
                  AND h.laser_x IS NOT NULL
                ORDER BY c.number
                """),
            {"t": tenant_id, "d": dive_id},
        )
    ).all()
    return AutomaticCalibrationInputs(
        camera_calibration_id=camera.id,
        camera_matrix=[[float(x) for x in r] for r in camera.camera_matrix],
        candidates=[
            AutomaticCalibrationCandidate(r.id, r.checksum, r.from_v1, r.laser_x,
                                          r.laser_y)  # fmt: skip
            for r in rows
            if slate.get(r.id, 0.0) >= SLATE_FRAME_THRESHOLD
        ],
        line_dots=[[r.laser_x, r.laser_y] for r in rows],
    )


@dataclass(frozen=True)
class AutomaticCalibrationRow:
    outcome: str
    algorithm_version: str
    refusal_reason: str | None = None
    camera_calibration_id: uuid.UUID | None = None
    laser_position: list[float] | None = None
    laser_axis: list[float] | None = None
    vanishing_px: float | None = None
    line_direction: list[float] | None = None
    line_offset_px: float | None = None
    o_mag_m: float | None = None
    frames_used: int = 0
    candidate_count: int = 0
    pair_count: int = 0
    size_ratio: float | None = None
    se_px: float | None = None
    pair_residual_sd: float | None = None
    capture_ids: list[uuid.UUID] = field(default_factory=list)
    core_version: str | None = None


async def persist_automatic_calibration(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    row: AutomaticCalibrationRow,
) -> uuid.UUID:
    """Append the dive's label-free calibration (or its refusal); the frames
    it names must be the dive's. Returns its id."""
    if row.capture_ids:
        await _check_captures(conn, tenant_id, dive_id, row.capture_ids)
    values = asdict(row)
    for key in ("laser_position", "laser_axis", "line_direction"):
        values[key] = None if values[key] is None else json.dumps(values[key])
    return (
        await conn.execute(
            text("""
                INSERT INTO automatic_laser_calibrations (
                    tenant_id, dive_id, outcome, algorithm_version, refusal_reason,
                    camera_calibration_id, laser_position, laser_axis, vanishing_px,
                    line_direction, line_offset_px, o_mag_m, frames_used,
                    candidate_count, pair_count, size_ratio, se_px,
                    pair_residual_sd, capture_ids, core_version)
                VALUES (:t, :d, :outcome, :algorithm_version, :refusal_reason,
                        :camera_calibration_id, CAST(:laser_position AS jsonb),
                        CAST(:laser_axis AS jsonb), :vanishing_px,
                        CAST(:line_direction AS jsonb), :line_offset_px, :o_mag_m,
                        :frames_used, :candidate_count, :pair_count, :size_ratio,
                        :se_px, :pair_residual_sd, :capture_ids, :core_version)
                RETURNING id
                """),
            {"t": tenant_id, "d": dive_id, **values},
        )
    ).scalar_one()


# -- measurement --------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementCalibration:
    """The calibration the dive's automatic lengths use now."""

    source: str
    automatic_laser_calibration_id: uuid.UUID | None
    laser_calibration_id: uuid.UUID | None
    laser_position: list[float]
    laser_axis: list[float]


@dataclass(frozen=True)
class AutomaticMeasureCapture:
    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    laser_x: float
    laser_y: float
    head_x: float
    head_y: float
    tail_x: float
    tail_y: float


@dataclass(frozen=True)
class AutomaticMeasureInputs:
    calibration: MeasurementCalibration | None
    camera_calibration_id: uuid.UUID | None
    camera_matrix: list[list[float]] | None
    captures: list[AutomaticMeasureCapture]


async def automatic_measure_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    algorithm_version: str,
    slate_frames: SlateFrames = no_slate_frames,
) -> AutomaticMeasureInputs:
    """The dive's predicted fish (never a slate frame, by its row or by the
    detector now) with no current automatic length at the current version,
    and the calibration to measure them with; nothing to measure without one."""
    cal = (
        await conn.execute(
            text("""
                SELECT * FROM automatic_measurement_calibrations
                WHERE tenant_id = :t AND dive_id = :d
                """),
            {"t": tenant_id, "d": dive_id},
        )
    ).one_or_none()
    if cal is None:
        return AutomaticMeasureInputs(None, None, None, [])
    rows = await conn.execute(
        text(f"""
            SELECT h.capture_id, h.id, h.laser_x, h.laser_y, h.head_x, h.head_y,
                   h.tail_x, h.tail_y
            FROM captures c
            JOIN current_automatic_head_tail_predictions h
              ON h.tenant_id = c.tenant_id AND h.capture_id = c.id
            LEFT JOIN current_automatic_measurements m
              ON m.tenant_id = c.tenant_id AND m.capture_id = c.id
             AND m.algorithm_version = :mv
            WHERE c.tenant_id = :t AND c.dive_id = :d AND c.is_canonical
              AND h.status = 'predicted' AND m.id IS NULL
              AND NOT {_calibration_session(":t", ":d")}
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": dive_id, "mv": algorithm_version},
    )
    slate = await _slate_probabilities(slate_frames, conn, tenant_id, dive_id)
    return AutomaticMeasureInputs(
        calibration=MeasurementCalibration(
            source=cal.calibration_source,
            automatic_laser_calibration_id=cal.automatic_laser_calibration_id,
            laser_calibration_id=cal.laser_calibration_id,
            laser_position=[float(v) for v in cal.laser_position],
            laser_axis=[float(v) for v in cal.laser_axis],
        ),
        camera_calibration_id=cal.camera_calibration_id,
        camera_matrix=[[float(x) for x in r] for r in cal.camera_matrix],
        captures=[
            AutomaticMeasureCapture(r.capture_id, r.id, r.laser_x, r.laser_y,
                                    r.head_x, r.head_y, r.tail_x, r.tail_y)  # fmt: skip
            for r in rows
            if slate.get(r.capture_id, 0.0) < SLATE_FRAME_THRESHOLD
        ],
    )


@dataclass(frozen=True)
class AutomaticMeasurementRow:
    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    calibration_source: str
    algorithm: str
    algorithm_version: str
    core_version: str
    automatic_laser_calibration_id: uuid.UUID | None = None
    laser_calibration_id: uuid.UUID | None = None
    camera_calibration_id: uuid.UUID | None = None
    length_m: float | None = None
    depth_m: float | None = None
    refusal: str | None = None


async def persist_automatic_measurements(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    rows: Sequence[AutomaticMeasurementRow],
) -> int:
    """Append automatic lengths (and refusals); each must name its own
    capture's automatic head/tail."""
    rows = list(rows)
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, dive_id, [r.capture_id for r in rows])
    await _check_head_tails(conn, tenant_id, rows)
    return len(await _insert(conn, "automatic_measurements", tenant_id, rows))


class AutomaticResultsCatalog(ServicePrincipal):
    """The automatic-results track's database side, as the orchestrator's
    service principal. `slate_frames` is the slate-presence detector's store
    function (`slate_presence_store.slate_frames` in production)."""

    def __init__(
        self, engine, *, sub: str, slate_frames: SlateFrames = no_slate_frames
    ):
        super().__init__(engine, sub=sub)
        self._slate_frames = slate_frames

    async def next_dive_for_automatic_results(
        self, tenant_id: uuid.UUID, **versions
    ) -> AutomaticCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_automatic_results(conn, tenant_id, **versions)

    async def record_automatic_results_failure(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, step: str, error: str
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await record_automatic_results_failure(
                conn, tenant_id, dive_id, step=step, error=error
            )

    async def record_automatic_frame_failures(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        failures: Sequence[tuple[uuid.UUID, str]],
        *,
        headtail_version: int,
    ) -> list[uuid.UUID]:
        async with self._tenant(tenant_id) as conn:
            return await record_automatic_frame_failures(
                conn, tenant_id, dive_id, failures, headtail_version=headtail_version
            )

    async def automatic_frames_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, headtail_version: int
    ) -> AutomaticFramesInputs:
        async with self._tenant(tenant_id) as conn:
            return await automatic_frames_inputs(
                conn, tenant_id, dive_id, headtail_version=headtail_version,
                slate_frames=self._slate_frames,
            )  # fmt: skip

    async def persist_automatic_head_tails(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, rows
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_head_tails(conn, tenant_id, dive_id, rows)

    async def automatic_species_captures(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, species_version: int
    ) -> list[AutomaticSpeciesCapture]:
        async with self._tenant(tenant_id) as conn:
            return await automatic_species_captures(
                conn, tenant_id, dive_id, species_version=species_version
            )

    async def persist_automatic_species(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, rows
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_species(conn, tenant_id, dive_id, rows)

    async def automatic_calibration_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> AutomaticCalibrationInputs:
        async with self._tenant(tenant_id) as conn:
            return await automatic_calibration_inputs(
                conn, tenant_id, dive_id, slate_frames=self._slate_frames
            )

    async def persist_automatic_calibration(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, row: AutomaticCalibrationRow
    ) -> uuid.UUID:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_calibration(conn, tenant_id, dive_id, row)

    async def automatic_measure_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, algorithm_version: str
    ) -> AutomaticMeasureInputs:
        async with self._tenant(tenant_id) as conn:
            return await automatic_measure_inputs(
                conn, tenant_id, dive_id, algorithm_version=algorithm_version,
                slate_frames=self._slate_frames,
            )  # fmt: skip

    async def persist_automatic_measurements(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, rows
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_measurements(conn, tenant_id, dive_id, rows)
