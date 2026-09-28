"""The database side of the laser-depth stage, tenant-scoped.

Ported from fishsense-lite@77e8f8e5: the cohort is the API's
`select-next/laser-depth` endpoint (dive_cohort_controller.py
`_laser_depth_cohort_query`); the inputs are what
`compute_laser_depths_activity` read through `load_dive_calibration_context`
(dive -> camera intrinsics -> resolved extrinsics) and the dive's laser labels
and depths; the write is its `put_laser_depth`. v1's rules, kept:

* a capture needs a depth when it is canonical, carries a valid laser label
  (completed, not superseded, x and y set), and has no *current* depth -- one
  naming a still-valid label **of the same capture** under the calibration
  the dive resolves to today. Keyed on the capture, not the label: 461 prod
  images carry two valid labels, and the label-keyed version wedged dive 279;
* a capture's labels are tried in ascending order (v1's id; v2's number,
  which is v1's id for a migrated label), the first that triangulates wins.

The rules live in migration 0026's views (`dive_laser_geometry`,
`laser_depth_work`); the cohort, the resolver and the persist check all read
them, so they cannot disagree.

v2 changes:

* per tenant, ordered by `created_at` then `number` (v1: `id`, which
  `number` is for a migrated dive), so the orchestrator takes
  the oldest candidate across the tenants it serves;
* the calibration is 0018's effective one (own and plausible, else the
  link's), and the camera matrix is the current calibration of the dive's
  device; a dive the kernel cannot project (no pinhole calibration, a
  singular matrix, a directionless axis) is not offered, where v1 raised
  for an hour at the head of its cohort;
* **tried, made no progress** (PLAN.md §9.16): a refusal is recorded per label
  tried, and a capture whose every valid label is refused under the effective
  calibration is no longer work -- until the calibration, the dot or the
  labels change. v1 wrote nothing and re-selected the dive forever (dive 32);
* **appended, never overwritten** (v1 upserted on the image): a recompute is a
  new row, and a persist is checked against the work it answers, so a retry
  writes nothing twice and output that answers no current work is dropped
  (PLAN.md §9.11).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "CaptureDots",
    "DepthRecord",
    "DepthRefusal",
    "DiveGeometry",
    "Dot",
    "LaserDepthCandidate",
    "LaserDepthCatalog",
    "LaserDepthWork",
    "PersistedDepths",
    "dive_geometry",
    "laser_depth_work",
    "lock_dive",
    "next_dive_for_laser_depth",
    "persist_laser_depths",
]

Vector3 = tuple[float, float, float]


@dataclass(frozen=True)
class LaserDepthCandidate:
    dive_id: uuid.UUID
    created_at: datetime
    #: The tiebreak: v1's id for a migrated dive, and every migrated dive
    #: shares one created_at (v1 recorded none), so the UUID would drain
    #: them in random order.
    number: int


@dataclass(frozen=True)
class DiveGeometry:
    """What a dive is measured with: its effective laser calibration and the
    current camera calibration of its device."""

    laser_calibration_id: uuid.UUID
    laser_position: Vector3
    laser_axis: Vector3
    camera_calibration_id: uuid.UUID
    camera_matrix: tuple[Vector3, Vector3, Vector3]


@dataclass(frozen=True)
class Dot:
    laser_label_id: uuid.UUID
    x: float
    y: float


@dataclass(frozen=True)
class CaptureDots:
    capture_id: uuid.UUID
    #: The capture's valid, unrefused labels, in the order to try them.
    laser_labels: list[Dot]


@dataclass(frozen=True)
class LaserDepthWork:
    """A dive's laser-depth work, and what it skipped (v1's counters)."""

    geometry: DiveGeometry | None
    captures: list[CaptureDots]
    #: Canonical captures with a valid label whose depth is current.
    skipped_current: int
    #: ... with no current depth, whose every valid label is refused (v2).
    skipped_refused: int
    #: Live labels on canonical captures that are not a validated fix.
    skipped_unusable_label: int


@dataclass(frozen=True)
class DepthRecord:
    capture_id: uuid.UUID
    laser_label_id: uuid.UUID
    #: The dot the depth was computed at, echoed back: Label Studio sync moves
    #: a dot in place (same label id), so the id alone cannot tell a depth at
    #: the old pixel from one at the new.
    x: float
    y: float
    depth_m: float
    range_m: float
    residual_m: float | None


@dataclass(frozen=True)
class DepthRefusal:
    capture_id: uuid.UUID
    laser_label_id: uuid.UUID
    x: float
    y: float
    reason: str
    depth_m: float | None


@dataclass(frozen=True)
class PersistedDepths:
    written: int
    refused: int
    #: Results that answered no current work: a retry's, or the world moved
    #: on (a label superseded, a new calibration) since the work was resolved.
    skipped_stale: int


def _vector(value) -> Vector3:
    return tuple(float(v) for v in value)


async def next_dive_for_laser_depth(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> LaserDepthCandidate | None:
    """The tenant's oldest high-priority dive with laser-depth work."""
    row = (
        await conn.execute(
            text("""
                SELECT d.id, d.created_at, d.number FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND EXISTS (
                      SELECT 1 FROM laser_depth_work w
                      WHERE w.tenant_id = d.tenant_id AND w.dive_id = d.id
                  )
                ORDER BY d.created_at, d.number
                LIMIT 1
                """),
            {"tenant": tenant_id},
        )
    ).one_or_none()
    return (
        None if row is None else LaserDepthCandidate(row.id, row.created_at, row.number)
    )


async def dive_geometry(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> DiveGeometry | None:
    """The dive's effective calibration and camera matrix, if it can be
    projected at all."""
    row = (
        await conn.execute(
            text("""
                SELECT laser_calibration_id, laser_position, laser_axis,
                       camera_calibration_id, camera_matrix
                FROM dive_laser_geometry
                WHERE tenant_id = :tenant AND dive_id = :dive
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return DiveGeometry(
        laser_calibration_id=row.laser_calibration_id,
        laser_position=_vector(row.laser_position),
        laser_axis=_vector(row.laser_axis),
        camera_calibration_id=row.camera_calibration_id,
        camera_matrix=tuple(_vector(r) for r in row.camera_matrix),
    )


async def laser_depth_work(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> LaserDepthWork:
    """What the processor is given for the dive: per capture needing a depth,
    its valid unrefused labels in label order."""
    geometry = await dive_geometry(conn, tenant_id, dive_id)
    rows = await conn.execute(
        text("""
            SELECT capture_id, laser_label_id, x, y FROM laser_depth_work
            WHERE tenant_id = :tenant AND dive_id = :dive
            ORDER BY capture_number, laser_label_number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    captures: dict[uuid.UUID, list[Dot]] = {}
    for r in rows:
        captures.setdefault(r.capture_id, []).append(Dot(r.laser_label_id, r.x, r.y))

    # The counters are diagnostics (v1: "a green child is not proof the work
    # was done -- read its counters"); what is work is the view's alone.
    counts = (
        await conn.execute(
            text("""
                WITH labelled AS (
                    SELECT c.id,
                           bool_or(l.completed AND l.x IS NOT NULL
                                   AND l.y IS NOT NULL) AS has_valid,
                           count(*) FILTER (
                               WHERE NOT (l.completed AND l.x IS NOT NULL
                                          AND l.y IS NOT NULL)
                           ) AS unusable,
                           EXISTS (
                               SELECT 1 FROM laser_depth_work w
                               WHERE w.tenant_id = c.tenant_id AND w.capture_id = c.id
                           ) AS open,
                           EXISTS (
                               SELECT 1 FROM current_laser_depths cd
                               JOIN laser_labels rl
                                 ON rl.tenant_id = cd.tenant_id
                                AND rl.id = cd.laser_label_id
                               WHERE cd.tenant_id = c.tenant_id AND cd.capture_id = c.id
                                 AND cd.laser_calibration_id
                                     = CAST(:calibration AS uuid)
                                 AND rl.capture_id = c.id AND rl.completed
                                 AND NOT rl.superseded
                                 AND rl.x IS NOT NULL AND rl.y IS NOT NULL
                           ) AS current
                    FROM captures c
                    JOIN laser_labels l
                      ON l.tenant_id = c.tenant_id AND l.capture_id = c.id
                    WHERE c.tenant_id = :tenant AND c.dive_id = :dive
                      AND c.is_canonical AND NOT l.superseded
                    GROUP BY c.id, c.tenant_id
                )
                SELECT count(*) FILTER (WHERE has_valid AND current) AS current,
                       count(*) FILTER (
                           WHERE has_valid AND NOT current AND NOT open
                       ) AS refused,
                       coalesce(sum(unusable), 0) AS unusable
                FROM labelled
                """),
            {
                "tenant": tenant_id,
                "dive": dive_id,
                "calibration": (
                    None if geometry is None else geometry.laser_calibration_id
                ),
            },
        )
    ).one()
    if geometry is None:
        # Nothing is work without a calibration to work under.
        return LaserDepthWork(None, [], 0, 0, int(counts.unusable))
    return LaserDepthWork(
        geometry=geometry,
        captures=[CaptureDots(capture, dots) for capture, dots in captures.items()],
        skipped_current=int(counts.current),
        skipped_refused=int(counts.refused),
        skipped_unusable_label=int(counts.unusable),
    )


async def lock_dive(conn: AsyncConnection, stage: str, dive_id: uuid.UUID) -> None:
    """Serialise one stage's persists of one dive, so two can't both find the
    same work still open."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{stage}:{dive_id}"},
    )


async def _is_work(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    laser_calibration_id: uuid.UUID,
    capture_id: uuid.UUID,
    laser_label_id: uuid.UUID,
    dot: tuple[float, float] | None = None,
) -> bool:
    """Is this (capture, label) -- with this dot, when given -- still open
    work of this dive under this calibration?"""
    return (
        await conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM laser_depth_work
                    WHERE tenant_id = :tenant AND dive_id = :dive
                      AND capture_id = :capture AND laser_label_id = :label
                      AND laser_calibration_id = :calibration
                      AND (NOT CAST(:check_dot AS boolean)
                           OR (x = CAST(:x AS double precision)
                               AND y = CAST(:y AS double precision)))
                )
                """),
            {
                "tenant": tenant_id,
                "dive": dive_id,
                "capture": capture_id,
                "label": laser_label_id,
                "calibration": laser_calibration_id,
                "check_dot": dot is not None,
                "x": None if dot is None else dot[0],
                "y": None if dot is None else dot[1],
            },
        )
    ).scalar_one()


async def persist_laser_depths(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    laser_calibration_id: uuid.UUID,
    core_version: str,
    depths: list[DepthRecord],
    refusals: list[DepthRefusal],
    run_id: uuid.UUID | None = None,
) -> PersistedDepths:
    """Append the processor's depths and refusals for the dive, in the
    caller's transaction. Each is written only if it still answers open
    work: refusals first, since a capture's depth closes its work."""
    await lock_dive(conn, "laser-depth", dive_id)
    written = refused = stale = 0
    for refusal in refusals:
        if not await _is_work(
            conn,
            tenant_id,
            dive_id,
            laser_calibration_id,
            refusal.capture_id,
            refusal.laser_label_id,
            dot=(refusal.x, refusal.y),
        ):
            stale += 1
            continue
        await conn.execute(
            text("""
                INSERT INTO laser_depth_refusals
                    (tenant_id, capture_id, laser_label_id, laser_x, laser_y,
                     laser_calibration_id, reason, depth_m, core_version, run_id)
                VALUES (:tenant, :capture, :label, :x, :y, :calibration, :reason,
                        :depth, :core, :run)
                """),
            {
                "tenant": tenant_id,
                "capture": refusal.capture_id,
                "label": refusal.laser_label_id,
                "x": refusal.x,
                "y": refusal.y,
                "calibration": laser_calibration_id,
                "reason": refusal.reason,
                "depth": refusal.depth_m,
                "core": core_version,
                "run": run_id,
            },
        )
        refused += 1
    for record in depths:
        if not await _is_work(
            conn,
            tenant_id,
            dive_id,
            laser_calibration_id,
            record.capture_id,
            record.laser_label_id,
            dot=(record.x, record.y),
        ):
            stale += 1
            continue
        await conn.execute(
            text("""
                INSERT INTO laser_depths
                    (tenant_id, capture_id, laser_label_id, laser_calibration_id,
                     depth_m, range_m, residual_m, core_version)
                VALUES (:tenant, :capture, :label, :calibration, :depth, :range,
                        :residual, :core)
                """),
            {
                "tenant": tenant_id,
                "capture": record.capture_id,
                "label": record.laser_label_id,
                "calibration": laser_calibration_id,
                "depth": record.depth_m,
                "range": record.range_m,
                "residual": record.residual_m,
                "core": core_version,
            },
        )
        written += 1
    return PersistedDepths(written=written, refused=refused, skipped_stale=stale)


class LaserDepthCatalog(ServicePrincipal):
    """The laser-depth stage's database side, as the orchestrator's service
    principal."""

    async def next_dive_for_laser_depth(
        self, tenant_id: uuid.UUID
    ) -> LaserDepthCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_laser_depth(conn, tenant_id)

    async def laser_depth_work(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> LaserDepthWork:
        async with self._tenant(tenant_id) as conn:
            return await laser_depth_work(conn, tenant_id, dive_id)

    async def persist_laser_depths(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, **result
    ) -> PersistedDepths:
        async with self._tenant(tenant_id) as conn:
            return await persist_laser_depths(conn, tenant_id, dive_id, **result)
