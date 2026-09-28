"""The database side of stage 14 (measure fish), tenant-scoped.

Ported from fishsense-lite@77e8f8e5: the cohort is the API's
`select-next/measure-fish` endpoint (dive_cohort_controller.py
`select_next_for_measure_fish`), and the per-image decisions are
measure_fish_activity.py's -- which top-three species labels are measurable
(a `Common (Scientific)` real fish, a `Fish Model, <name>`, the ruler or the
box), the Label Studio cluster gate for real fish, the "already measured under
the current calibration" skip, `_ensure_species`, `_ensure_fish` (reuse the
cluster's fish, else create one and bind the cluster) and `_ensure_model_fish`
(one fish per model name). The geometry is the processor's.

What is work is migration 0026's `measurement_work` view; the
cohort, the resolver and the persist check all read it, so they cannot
disagree (v1's dives 32, 279 and 466, and the `Fish Model,` empty leaf, were
each a disagreement between its cohort SQL and its activity).

v2 changes:

* per tenant, ordered by `created_at` then `number` (v1: `id`, which
  `number` is for a migrated dive); the orchestrator takes the
  oldest candidate across the tenants it serves;
* one species label per capture: live, not a sentinel, the highest-numbered.
  And one laser label and one head/tail label: the lowest-numbered valid ones
  (v1's activity read "the first non-superseded", with no order);
* **measurements are appended**; v1's stale-binding DELETE is
  `current_measurements`' rule (0026), so a re-bound capture is
  simply work again. Like the DELETE, it holds only on high-priority dives
  -- the ones stage 14 re-measures (0028);
* **tried, made no progress** (PLAN.md §9.16): a zero or non-finite length, or
  a real-fish leaf no name can be read from, is recorded as a refusal of those
  inputs, which closes the work until one of them changes;
* species and fish models are found or created with `ensure_species` and
  `ensure_fish_model` (0027) -- global tables the app role cannot
  write -- and only for a length that is actually written (v1 created the
  species, fish and cluster binding before computing the length, so a NaN
  frame still created them);
* each length is checked against the work it answers (PLAN.md §9.11): a retry
  writes nothing twice, and a length for inputs that changed since is dropped.

The species names are read by the caller's `species_names` -- the contracts'
`parse_species_names`, v1's definition of record -- because this package does
not depend on the contracts package.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.laser_depth_store import (
    DiveGeometry,
    Dot,
    dive_geometry,
    lock_dive,
)
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "MEASUREMENT_COHORT",
    "HeadTailPoints",
    "LengthRecord",
    "MeasureCapture",
    "MeasurementCandidate",
    "MeasurementCatalog",
    "MeasurementWork",
    "PersistedMeasurements",
    "SpeciesNames",
    "measurement_work",
    "next_dive_for_measurement",
    "persist_measurements",
]

#: The cohort over dive `d`, but for the tenant and priority terms the
#: selector adds: the dive has a row of migration 0026's `measurement_work`.
#: Named so `dive_pipeline_status` reads the same predicate (migration
#: 0029).
MEASUREMENT_COHORT = """EXISTS (
    SELECT 1 FROM measurement_work w
    WHERE w.tenant_id = d.tenant_id AND w.dive_id = d.id
)"""

#: `content_of_image` -> (common name, scientific name), or None.
SpeciesNames = Callable[[str | None], tuple[str, str] | None]


@dataclass(frozen=True)
class MeasurementCandidate:
    dive_id: uuid.UUID
    created_at: datetime
    #: The tiebreak: v1's id for a migrated dive, and every migrated dive
    #: shares one created_at (v1 recorded none), so the UUID would drain
    #: them in random order.
    number: int


@dataclass(frozen=True)
class HeadTailPoints:
    head_tail_label_id: uuid.UUID
    head_x: float
    head_y: float
    tail_x: float
    tail_y: float


@dataclass(frozen=True)
class MeasureCapture:
    """One capture to measure, and what its length will be bound to."""

    capture_id: uuid.UUID
    species_label_id: uuid.UUID
    content_of_image: str | None
    real_fish: bool
    #: The rigid target's name, when it is not a real fish.
    model_name: str | None
    cluster_id: uuid.UUID | None
    cluster_fish_id: uuid.UUID | None
    laser: Dot
    head_tail: HeadTailPoints


@dataclass(frozen=True)
class MeasurementWork:
    """A dive's stage-14 work, and v1's counters for what it skipped."""

    geometry: DiveGeometry | None
    captures: list[MeasureCapture]
    skipped_already_measured: int
    skipped_unmeasurable_species: int
    missing_cluster: int
    missing_laser_or_headtail: int
    #: v2: measurable, but these very inputs were tried and refused.
    skipped_refused: int


@dataclass(frozen=True)
class LengthRecord:
    """The processor's answer for one capture, echoing its inputs."""

    capture_id: uuid.UUID
    species_label_id: uuid.UUID
    laser: Dot
    head_tail: HeadTailPoints
    length_m: float | None
    depth_m: float | None
    refusal: str | None


@dataclass(frozen=True)
class PersistedMeasurements:
    measured: int
    refused: int
    #: Lengths that answered no current work (a retry's, or inputs changed).
    skipped_stale: int
    fish_created: int
    clusters_bound: int


_WORK_COLUMNS = """
    capture_id, laser_calibration_id, species_label_id, content_of_image,
    real_fish, model_name, cluster_id, cluster_fish_id, laser_label_id,
    laser_x, laser_y, head_tail_label_id, head_x, head_y, tail_x, tail_y
"""


def _capture(row) -> MeasureCapture:
    return MeasureCapture(
        capture_id=row.capture_id,
        species_label_id=row.species_label_id,
        content_of_image=row.content_of_image,
        real_fish=row.real_fish,
        model_name=row.model_name,
        cluster_id=row.cluster_id,
        cluster_fish_id=row.cluster_fish_id,
        laser=Dot(row.laser_label_id, row.laser_x, row.laser_y),
        head_tail=HeadTailPoints(
            row.head_tail_label_id, row.head_x, row.head_y, row.tail_x, row.tail_y
        ),
    )


async def next_dive_for_measurement(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> MeasurementCandidate | None:
    """The tenant's oldest high-priority dive with stage-14 work."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at, d.number FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND {MEASUREMENT_COHORT}
                ORDER BY d.created_at, d.number
                LIMIT 1
                """),
            {"tenant": tenant_id},
        )
    ).one_or_none()
    return (
        None
        if row is None
        else MeasurementCandidate(row.id, row.created_at, row.number)
    )


async def measurement_work(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> MeasurementWork:
    """What the processor is given for the dive, in capture order."""
    geometry = await dive_geometry(conn, tenant_id, dive_id)
    rows = await conn.execute(
        text(f"""
            SELECT {_WORK_COLUMNS} FROM measurement_work
            WHERE tenant_id = :tenant AND dive_id = :dive
            ORDER BY capture_number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    captures = [_capture(r) for r in rows]

    # Diagnostics, in v1's counters; what is work is the view's alone.
    counts = (
        await conn.execute(
            text("""
                WITH subjects AS (
                    SELECT c.id,
                           s.real_fish OR s.model_name IS NOT NULL AS measurable,
                           NOT s.real_fish OR s.cluster_id IS NOT NULL AS clustered,
                           EXISTS (
                               SELECT 1 FROM laser_labels l
                               WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
                                 AND l.completed AND NOT l.superseded
                                 AND l.x IS NOT NULL AND l.y IS NOT NULL
                           ) AND EXISTS (
                               SELECT 1 FROM head_tail_labels h
                               WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                                 AND h.completed AND NOT h.superseded
                                 AND h.head_x IS NOT NULL AND h.head_y IS NOT NULL
                                 AND h.tail_x IS NOT NULL AND h.tail_y IS NOT NULL
                           ) AS labelled,
                           EXISTS (
                               SELECT 1 FROM current_measurements m
                               WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                                 AND m.source = 'server'
                           ) AS measured,
                           EXISTS (
                               SELECT 1 FROM measurement_work w
                               WHERE w.tenant_id = c.tenant_id AND w.capture_id = c.id
                           ) AS open
                    FROM captures c
                    JOIN measurement_subjects s
                      ON s.tenant_id = c.tenant_id AND s.capture_id = c.id
                    WHERE c.tenant_id = :tenant AND c.dive_id = :dive
                      AND c.is_canonical AND s.top_three
                )
                SELECT
                    count(*) FILTER (WHERE NOT measurable) AS unmeasurable,
                    count(*) FILTER (WHERE measurable AND NOT clustered)
                        AS missing_cluster,
                    count(*) FILTER (WHERE measurable AND clustered AND NOT labelled)
                        AS missing_labels,
                    count(*) FILTER (
                        WHERE measurable AND clustered AND labelled AND measured
                    ) AS measured,
                    count(*) FILTER (
                        WHERE measurable AND clustered AND labelled AND NOT measured
                          AND NOT open
                    ) AS refused
                FROM subjects
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).one()
    return MeasurementWork(
        geometry=geometry,
        captures=captures if geometry is not None else [],
        skipped_already_measured=int(counts.measured),
        skipped_unmeasurable_species=int(counts.unmeasurable),
        missing_cluster=int(counts.missing_cluster),
        missing_laser_or_headtail=int(counts.missing_labels),
        skipped_refused=int(counts.refused) if geometry is not None else 0,
    )


async def _open_work(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    laser_calibration_id: uuid.UUID,
    record: LengthRecord,
) -> MeasureCapture | None:
    """The capture's open work, if this length answers exactly it."""
    row = (
        await conn.execute(
            text(f"""
                SELECT {_WORK_COLUMNS} FROM measurement_work
                WHERE tenant_id = :tenant AND dive_id = :dive
                  AND capture_id = :capture
                """),
            {"tenant": tenant_id, "dive": dive_id, "capture": record.capture_id},
        )
    ).one_or_none()
    if row is None or row.laser_calibration_id != laser_calibration_id:
        return None
    work = _capture(row)
    if (
        work.species_label_id != record.species_label_id
        or work.laser != record.laser
        or work.head_tail != record.head_tail
    ):
        return None
    return work


async def _refuse(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    laser_calibration_id: uuid.UUID,
    work: MeasureCapture,
    record: LengthRecord,
    reason: str,
    provenance: dict,
) -> None:
    await conn.execute(
        text("""
            INSERT INTO measurement_refusals
                (tenant_id, capture_id, laser_calibration_id, reason,
                 species_label_id, content_of_image, laser_label_id, laser_x,
                 laser_y, head_tail_label_id, head_x, head_y, tail_x, tail_y,
                 length_m, depth_m, algorithm, algorithm_version, core_version,
                 run_id)
            VALUES (:tenant, :capture, :calibration, :reason, :species_label,
                    :content, :laser, :laser_x, :laser_y, :head_tail, :head_x,
                    :head_y, :tail_x, :tail_y, :length, :depth, :algorithm,
                    :algorithm_version, :core_version, :run_id)
            """),
        {
            "tenant": tenant_id,
            "capture": work.capture_id,
            "calibration": laser_calibration_id,
            "reason": reason,
            "species_label": work.species_label_id,
            "content": work.content_of_image,
            "laser": work.laser.laser_label_id,
            "laser_x": work.laser.x,
            "laser_y": work.laser.y,
            "head_tail": work.head_tail.head_tail_label_id,
            "head_x": work.head_tail.head_x,
            "head_y": work.head_tail.head_y,
            "tail_x": work.head_tail.tail_x,
            "tail_y": work.head_tail.tail_y,
            "length": record.length_m,
            "depth": record.depth_m,
            **provenance,
        },
    )


async def _real_fish(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    cluster_id: uuid.UUID,
    names: tuple[str, str],
) -> tuple[uuid.UUID, bool]:
    """v1's `_ensure_fish`: the cluster's fish, or a new fish of the species,
    bound to the cluster. Returns (fish, created)."""
    bound = (
        await conn.execute(
            text("""
                SELECT fish_id FROM dive_frame_clusters
                WHERE tenant_id = :tenant AND id = :cluster
                FOR UPDATE
                """),
            {"tenant": tenant_id, "cluster": cluster_id},
        )
    ).scalar_one()
    if bound is not None:
        return bound, False
    common, scientific = names
    species_id = (
        await conn.execute(
            text("SELECT ensure_species(:scientific, :common)"),
            {"scientific": scientific, "common": common},
        )
    ).scalar_one()
    fish_id = (
        await conn.execute(
            text("""
                INSERT INTO fish (tenant_id, species_id) VALUES (:tenant, :species)
                RETURNING id
                """),
            {"tenant": tenant_id, "species": species_id},
        )
    ).scalar_one()
    await conn.execute(
        text("""
            UPDATE dive_frame_clusters SET fish_id = :fish, updated_at = now()
            WHERE tenant_id = :tenant AND id = :cluster
            """),
        {"tenant": tenant_id, "cluster": cluster_id, "fish": fish_id},
    )
    return fish_id, True


async def _model_fish(
    conn: AsyncConnection, tenant_id: uuid.UUID, name: str
) -> tuple[uuid.UUID, bool]:
    """v1's `_ensure_model_fish`: one fish per model name (per tenant here).
    Returns (fish, created)."""
    model_id = (
        await conn.execute(text("SELECT ensure_fish_model(:name)"), {"name": name})
    ).scalar_one()
    created = (
        await conn.execute(
            text("""
                INSERT INTO fish (tenant_id, fish_model_id) VALUES (:tenant, :model)
                ON CONFLICT (tenant_id, fish_model_id) DO NOTHING
                RETURNING id
                """),
            {"tenant": tenant_id, "model": model_id},
        )
    ).scalar_one_or_none()
    if created is not None:
        return created, True
    existing = (
        await conn.execute(
            text("""
                SELECT id FROM fish WHERE tenant_id = :tenant AND fish_model_id = :model
                """),
            {"tenant": tenant_id, "model": model_id},
        )
    ).scalar_one()
    return existing, False


async def persist_measurements(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    laser_calibration_id: uuid.UUID,
    algorithm: str,
    algorithm_version: str,
    core_version: str,
    lengths: list[LengthRecord],
    species_names: SpeciesNames,
    run_id: uuid.UUID | None = None,
) -> PersistedMeasurements:
    """Bind each length to its fish and append it -- or record its refusal --
    in the caller's transaction, if it still answers the capture's open work.
    """
    await lock_dive(conn, "measure-fish", dive_id)
    provenance = {
        "algorithm": algorithm,
        "algorithm_version": algorithm_version,
        "core_version": core_version,
        "run_id": run_id,
    }
    measured = refused = stale = fish_created = clusters_bound = 0
    for record in lengths:
        work = await _open_work(conn, tenant_id, dive_id, laser_calibration_id, record)
        if work is None:
            stale += 1
            continue
        if record.refusal is not None or record.length_m is None:
            await _refuse(
                conn,
                tenant_id,
                laser_calibration_id,
                work,
                record,
                record.refusal or "non_finite_length",
                provenance,
            )
            refused += 1
            continue
        if work.real_fish:
            names = species_names(work.content_of_image)
            if names is None:
                await _refuse(
                    conn,
                    tenant_id,
                    laser_calibration_id,
                    work,
                    record,
                    "unparseable_species",
                    provenance,
                )
                refused += 1
                continue
            fish_id, created = await _real_fish(conn, tenant_id, work.cluster_id, names)
            clusters_bound += int(created)
        else:
            fish_id, created = await _model_fish(conn, tenant_id, work.model_name)
        fish_created += int(created)
        await conn.execute(
            text("""
                INSERT INTO measurements
                    (tenant_id, capture_id, fish_id, source, length_m,
                     laser_calibration_id, laser_label_id, head_tail_label_id,
                     algorithm, algorithm_version, core_version, run_id)
                VALUES (:tenant, :capture, :fish, 'server', :length, :calibration,
                        :laser, :head_tail, :algorithm, :algorithm_version,
                        :core_version, :run_id)
                """),
            {
                "tenant": tenant_id,
                "capture": work.capture_id,
                "fish": fish_id,
                "length": record.length_m,
                "calibration": laser_calibration_id,
                "laser": work.laser.laser_label_id,
                "head_tail": work.head_tail.head_tail_label_id,
                **provenance,
            },
        )
        measured += 1
    return PersistedMeasurements(
        measured=measured,
        refused=refused,
        skipped_stale=stale,
        fish_created=fish_created,
        clusters_bound=clusters_bound,
    )


class MeasurementCatalog(ServicePrincipal):
    """Stage 14's database side, as the orchestrator's service principal."""

    async def next_dive_for_measurement(
        self, tenant_id: uuid.UUID
    ) -> MeasurementCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_measurement(conn, tenant_id)

    async def measurement_work(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> MeasurementWork:
        async with self._tenant(tenant_id) as conn:
            return await measurement_work(conn, tenant_id, dive_id)

    async def persist_measurements(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, **result
    ) -> PersistedMeasurements:
        async with self._tenant(tenant_id) as conn:
            return await persist_measurements(conn, tenant_id, dive_id, **result)
