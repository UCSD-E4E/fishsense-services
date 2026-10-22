"""The shadow size-constancy laser calibration's database side (0060).

docs/shadow-size-constancy.md. What the stage reads, read-only, and the two
tables it writes; nothing here writes anywhere else, and nothing in production
reads these tables (pinned by tests/test_shadow_calibration_store.py), so
measurement output is identical with the stage on or off.

**What it reads, per mission:**

* **slate frames**: canonical captures with a live slate label (production's
  rule: ``NOT superseded``, as `laser_calibration_store.slate_calibration_
  inputs`), and those the slate-presence detector calls slate (p >= 0.5,
  `slate_frames`, injected as the automatic track's is), each with its source
  (``label``, ``detector`` or ``both``);
* **label dots**: each frame's lowest-numbered live laser label --
  production's `_LOWEST_LIVE_DOT` exactly (``NOT superseded``, x and y set, by
  number), plus the label's id and source: production does not filter by
  source, so ``auto_accept`` labels are in, and recorded as such;
* **detector dots**: the automatic track's current head/tail row at the
  current predictor version -- its dot and presence confidence (author,
  2026-10-21: production's human-path ``laser_predictions`` covers 128 of the
  pool missions' 1,596 frames) -- for the slate frames, and for every frame
  of the mission, the detector line's population;
* **the mission's line** (``current_mission_laser_lines``), its pinhole camera,
  and **the reference**: its ``effective_laser_calibrations`` row (own or
  borrowed) -- what production measures with.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.automatic_results_store import (
    SlateFrames,
    _pinhole_camera,
    no_slate_frames,
)
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "ShadowCalibrationCatalog",
    "ShadowCalibrationInputs",
    "ShadowCalibrationRow",
    "ShadowFrame",
    "ShadowFrameRow",
    "next_mission_for_shadow_calibration",
    "persist_shadow_calibrations",
    "shadow_calibration_inputs",
    "shadow_calibration_cohort",
]


@dataclass(frozen=True)
class ShadowFrame:
    """One slate frame and both of its dots (either may be missing)."""

    capture_id: uuid.UUID
    capture_number: int | None
    captured_at: datetime | None
    checksum: str
    frame_source: str
    slate_probability: float | None
    label_dot: tuple[float, float] | None
    label_id: uuid.UUID | None
    label_source: str | None
    detector_dot: tuple[float, float] | None
    detector_confidence: float | None
    prediction_id: uuid.UUID | None


@dataclass(frozen=True)
class ShadowCalibrationInputs:
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    #: ``(a, b, c)`` of ``a x + b y + c = 0``, or None.
    mission_line: tuple[float, float, float] | None
    reference_laser_calibration_id: uuid.UUID | None
    reference_borrowed: bool | None
    reference_position: list[float] | None
    reference_axis: list[float] | None
    frames: list[ShadowFrame]
    #: Every current automatic dot of the mission: ``(x, y, confidence)``.
    mission_detector_dots: list[tuple[float, float, float]]


#: A capture `c` with a live slate label (production's slate frames).
_LABELLED_SLATE = """
    EXISTS (
        SELECT 1 FROM slate_labels s
        WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
          AND NOT s.superseded
    )
"""

#: Each capture's lowest live laser label: laser_calibration_store's
#: `_LOWEST_LIVE_DOT`, with its id and source.
_LOWEST_LIVE_DOT_WITH_SOURCE = """
    SELECT DISTINCT ON (l.capture_id) l.capture_id, l.x, l.y, l.id, l.source
    FROM laser_labels l
    WHERE l.tenant_id = :t AND NOT l.superseded
      AND l.x IS NOT NULL AND l.y IS NOT NULL
    ORDER BY l.capture_id, l.number
"""


async def shadow_calibration_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    headtail_version: int,
    slate_frames: SlateFrames = no_slate_frames,
) -> ShadowCalibrationInputs:
    camera = await _pinhole_camera(conn, tenant_id, mission_id)
    detected = {c: float(p) for c, p in await slate_frames(conn, tenant_id, mission_id)}
    rows = (
        await conn.execute(
            text(f"""
                SELECT c.id, c.number, c.captured_at, c.checksum,
                       {_LABELLED_SLATE} AS labelled,
                       dot.x AS lx, dot.y AS ly, dot.id AS label_id,
                       dot.source AS label_source,
                       h.id AS prediction_id, h.laser_x, h.laser_y,
                       h.laser_confidence
                FROM captures c
                LEFT JOIN ({_LOWEST_LIVE_DOT_WITH_SOURCE}) dot
                  ON dot.capture_id = c.id
                LEFT JOIN LATERAL (
                    SELECT a.* FROM current_automatic_head_tail_predictions a
                    WHERE a.tenant_id = c.tenant_id AND a.capture_id = c.id
                      AND a.predictor_version = :hv
                    LIMIT 1
                ) h ON true
                WHERE c.tenant_id = :t AND c.mission_id = :d AND c.is_canonical
                ORDER BY c.number, c.id
                """),
            {"t": tenant_id, "d": mission_id, "hv": headtail_version},
        )
    ).all()
    frames, mission_dots = [], []
    for r in rows:
        if r.laser_x is not None:
            mission_dots.append(
                (float(r.laser_x), float(r.laser_y), float(r.laser_confidence or 0.0))
            )
        by_detector = r.id in detected
        if not (r.labelled or by_detector):
            continue
        frames.append(
            ShadowFrame(
                capture_id=r.id,
                capture_number=r.number,
                captured_at=r.captured_at,
                checksum=r.checksum,
                frame_source=("both" if r.labelled and by_detector
                              else "label" if r.labelled else "detector"),
                slate_probability=detected.get(r.id),
                label_dot=None if r.lx is None else (float(r.lx), float(r.ly)),
                label_id=r.label_id,
                label_source=r.label_source,
                detector_dot=(None if r.laser_x is None
                              else (float(r.laser_x), float(r.laser_y))),
                detector_confidence=(None if r.laser_confidence is None
                                     else float(r.laser_confidence)),
                prediction_id=r.prediction_id,
            )  # fmt: skip
        )
    line = (
        await conn.execute(
            text("""
                SELECT a, b, c FROM current_mission_laser_lines
                WHERE tenant_id = :t AND mission_id = :d
                """),
            {"t": tenant_id, "d": mission_id},
        )
    ).one_or_none()
    reference = (
        await conn.execute(
            text("""
                SELECT e.laser_calibration_id, e.borrowed, l.laser_position,
                       l.laser_axis
                FROM effective_laser_calibrations e
                JOIN laser_calibrations l
                  ON l.tenant_id = e.tenant_id AND l.id = e.laser_calibration_id
                WHERE e.tenant_id = :t AND e.mission_id = :d
                """),
            {"t": tenant_id, "d": mission_id},
        )
    ).one_or_none()
    return ShadowCalibrationInputs(
        camera_calibration_id=camera.id,
        camera_matrix=[[float(x) for x in row] for row in camera.camera_matrix],
        mission_line=None if line is None else (float(line.a), float(line.b),
                                                float(line.c)),  # fmt: skip
        reference_laser_calibration_id=None if reference is None
        else reference.laser_calibration_id,
        reference_borrowed=None if reference is None else bool(reference.borrowed),
        reference_position=None if reference is None
        else [float(v) for v in reference.laser_position],
        reference_axis=None if reference is None
        else [float(v) for v in reference.laser_axis],
        frames=frames,
        mission_detector_dots=mission_dots,
    )  # fmt: skip


# -- persisting ------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowFrameRow:
    capture_id: uuid.UUID
    frame_source: str
    used: bool
    rejected_reason: str | None = None
    slate_probability: float | None = None
    dot_x: float | None = None
    dot_y: float | None = None
    dot_label_id: uuid.UUID | None = None
    dot_label_source: str | None = None
    dot_prediction_id: uuid.UUID | None = None
    dot_confidence: float | None = None
    line_distance_px: float | None = None
    sam_prompt: str | None = None
    sam_score: float | None = None
    mask_area_px: float | None = None
    size: float | None = None
    t_px: float | None = None
    range_m: float | None = None
    capture_number: int | None = None
    captured_at: datetime | None = None


@dataclass(frozen=True)
class ShadowCalibrationRow:
    dot_source: str
    variant: str
    outcome: str
    o_mag_m: float
    frames_used: int
    sam_model: str
    sam_prompts: list[str]
    sam_threshold: float
    render_long_side: int
    algorithm_version: str
    code_version: str
    refusal_reason: str | None = None
    refusals: list[str] = field(default_factory=list)
    laser_position: list[float] | None = None
    laser_axis: list[float] | None = None
    vanishing_px: float | None = None
    line_direction: list[float] | None = None
    line_offset_px: float | None = None
    line_source: str | None = None
    camera_calibration_id: uuid.UUID | None = None
    reference_laser_calibration_id: uuid.UUID | None = None
    reference_borrowed: bool | None = None
    angle_diff_deg: float | None = None
    vp_offset_px: float | None = None
    vp_offset_deg_fx: float | None = None
    vp_offset_deg_f2850: float | None = None
    fit_residual: float | None = None
    se_px: float | None = None
    se_deg: float | None = None
    frames_rejected: dict[str, int] = field(default_factory=dict)
    size_spread: float | None = None
    range_min_m: float | None = None
    range_max_m: float | None = None
    lever_m: float | None = None
    lever_m_production_offset: float | None = None
    size_cv_pct: float | None = None
    size_maxdev_pct: float | None = None
    sam_score_min: float | None = None
    sam_score_median: float | None = None
    no_mask_share: float | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    frames: list[ShadowFrameRow] = field(default_factory=list)


_JSON = ("frames_rejected", "gate")


async def persist_shadow_calibrations(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    rows: Sequence[ShadowCalibrationRow],
) -> list[uuid.UUID]:
    """Append the mission's shadow results and their frames, all or nothing,
    in the caller's transaction. Frames must be the mission's captures."""
    import json  # pylint: disable=import-outside-toplevel

    rows = list(rows)
    captures = {f.capture_id for r in rows for f in r.frames}
    if captures:
        owned = set(
            (
                await conn.execute(
                    text("""
                        SELECT id FROM captures
                        WHERE tenant_id = :t AND mission_id = :d AND id = ANY(:ids)
                        """),
                    {"t": tenant_id, "d": mission_id, "ids": list(captures)},
                )
            ).scalars()
        )
        if foreign := captures - owned:
            raise ValueError(
                f"not captures of mission {mission_id}: {sorted(map(str, foreign))}"
            )
    ids = []
    for r in rows:
        values = {k: v for k, v in asdict(r).items() if k != "frames"}
        for k in _JSON:
            values[k] = json.dumps(values[k])
        columns = ", ".join(["tenant_id", "mission_id", *values])
        params = ", ".join(
            [
                ":t",
                ":d",
                *(f"CAST(:{k} AS jsonb)" if k in _JSON else f":{k}" for k in values),
            ]
        )
        rid = (
            await conn.execute(
                text(f"INSERT INTO shadow_laser_calibrations ({columns}) "
                     f"VALUES ({params}) RETURNING id"),
                {"t": tenant_id, "d": mission_id, **values},
            )
        ).scalar_one()  # fmt: skip
        ids.append(rid)
        for f in r.frames:
            fv = asdict(f)
            fcols = ", ".join(["tenant_id", "shadow_laser_calibration_id", *fv])
            fparams = ", ".join([":t", ":rid", *(f":{k}" for k in fv)])
            await conn.execute(
                text(
                    f"INSERT INTO shadow_calibration_frames ({fcols}) VALUES ({fparams})"
                ),
                {"t": tenant_id, "rid": rid, **fv},
            )
    return ids


# -- the cohort ----------------------------------------------------------------------


def shadow_calibration_cohort() -> str:
    """Mission `d` has a slate frame and no shadow result at `:av`. A frame
    the detector alone calls slate is found by the orchestrator per mission
    (`slate_frames`); the cohort selects on labelled slate frames and on the
    detector's own predicate, so both kinds enter."""
    from fishsense_services_api.slate_presence_store import (  # pylint: disable=import-outside-toplevel
        detector_slate_frame,
    )

    return f"""
        EXISTS (
            SELECT 1 FROM captures c
            WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id
              AND c.is_canonical
              AND ({_LABELLED_SLATE} OR {detector_slate_frame()})
        )
        AND NOT EXISTS (
            SELECT 1 FROM shadow_laser_calibrations x
            WHERE x.tenant_id = d.tenant_id AND x.mission_id = d.id
              AND x.algorithm_version = :av
        )
    """


async def next_mission_for_shadow_calibration(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    algorithm_version: str,
    exclude: Sequence[uuid.UUID] = (),
) -> uuid.UUID | None:
    """The next mission: those with a production calibration first (author,
    2026-10-21: calibrated missions first, then every mission with slate
    frames), oldest first."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id FROM missions d
                WHERE d.tenant_id = :t AND {shadow_calibration_cohort()}
                  AND NOT d.id = ANY(CAST(:exclude AS uuid[]))
                ORDER BY EXISTS (
                    SELECT 1 FROM effective_laser_calibrations e
                    WHERE e.tenant_id = d.tenant_id AND e.mission_id = d.id
                ) DESC, d.created_at, d.id
                LIMIT 1
                """),
            {"t": tenant_id, "av": algorithm_version, "exclude": list(exclude)},
        )
    ).one_or_none()
    return None if row is None else row.id


class ShadowCalibrationCatalog(ServicePrincipal):
    """The shadow stage's database side, as the orchestrator's service
    principal. `slate_frames` is the slate-presence detector's store function
    (`slate_presence_store.slate_frames` in production)."""

    def __init__(
        self, engine, *, sub: str, slate_frames: SlateFrames = no_slate_frames
    ):
        super().__init__(engine, sub=sub)
        self._slate_frames = slate_frames

    async def next_mission_for_shadow_calibration(
        self, tenant_id: uuid.UUID, *, algorithm_version: str, exclude=()
    ) -> uuid.UUID | None:
        async with self._tenant(tenant_id) as conn:
            return await next_mission_for_shadow_calibration(
                conn, tenant_id, algorithm_version=algorithm_version, exclude=exclude
            )

    async def shadow_calibration_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, headtail_version: int
    ) -> ShadowCalibrationInputs:
        async with self._tenant(tenant_id) as conn:
            return await shadow_calibration_inputs(
                conn, tenant_id, mission_id, headtail_version=headtail_version,
                slate_frames=self._slate_frames,
            )  # fmt: skip

    async def persist_shadow_calibrations(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, rows
    ) -> list[uuid.UUID]:
        async with self._tenant(tenant_id) as conn:
            return await persist_shadow_calibrations(conn, tenant_id, mission_id, rows)
