"""The database side of the species stages, tenant-scoped.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/src/fishsense_api/
controllers/: the stage-2 cohort (`select_next_for_species_preprocessing`)
and the species-population cohort (`select_dives_needing_species_population`)
in dive_cohort_controller.py; `_set_needs_reprocess` (species) in
label_reprocess_controller.py; the species-label reads and upsert
(`get_species_labels_for_dive`, `put_species_label`) in label_controller.py;
`get_clusters` / `post_cluster` in image_controller.py; and `set_dive_slate`,
`set_dive_calibration_target`, `set_notes` and `_clear_refusal` in
dive_controller.py. The workers' SDK reads that fed stage 2, populate and
stage 6.1 are single queries here, and their filtering stays in the
orchestrator where v1's tests pin it.

v1's rules, kept:

* **the stage-2 cohort**: a high-priority dive with a canonical capture that
  carries a valid laser label, has no live (not superseded) species row in a
  Label Studio project, and is in a prediction cluster -- or a canonical
  capture whose live species row is flagged `needs_reprocess`;
* **the population cohort**: the same without the cluster gate or the flag,
  every matching dive;
* a sentinel (a species row with no project) is not a label, and a
  superseded row is not evidence of done work;
* raising the flag touches only live, by default incomplete, canonical rows;
  clearing touches every canonical row of the dive (or only the named frames);
* the unidentified-slate note is written only when the dive has none, and
  never touches priority.

v2 changes:

* per tenant, ordered by `created_at` (v1: `id`);
* **populate's candidates are canonical** (v1 took every laser-valid image of
  the dive): a duplicate frame shares its twin's JPEG and task URL, and would
  have been anchored to the twin's task;
* **order is stated.** Prediction clusters come ordered by their earliest
  member's `captured_at` (then `number`), members by `captured_at`, `number`.
  v1's cluster read had no ORDER BY, yet "image i of N" and stage 6.1's
  "Part of previous group" both read it;
* **stage 6.1 writes all or nothing**, serialised per dive, and refuses a
  capture that is not a canonical capture of the dive (v1 posted cluster by
  cluster, so a failure left a partial set that blocked every re-run);
* **writing a link expires a refusal instead of clearing it**: v2's refusal
  is an append-only `laser_calibrations` row, so the write stamps
  `dives.calibration_links_changed_at` (migration 0022) and a refused
  row older than the stamp no longer stands (`REFUSAL_OUTLIVED_SQL`);
* calibration targets are versioned by `valid_from`, so a name resolves to its
  current row.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.clustering_store import (
    VALID_LASER,
    ForeignCapture,
    InvalidClusters,
)
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "REFUSAL_OUTLIVED_SQL",
    "CameraIntrinsicsRow",
    "SpeciesCandidate",
    "SpeciesCapture",
    "SpeciesCatalog",
    "SpeciesGroupingFacts",
    "SpeciesLabelRow",
    "SpeciesPopulationFacts",
    "SpeciesPreprocessFacts",
    "calibration_targets_by_name",
    "clear_dive_calibration_target",
    "dives_needing_species_population",
    "next_dive_for_species_preprocessing",
    "note_unidentified_slate",
    "persist_label_studio_clusters",
    "record_species_label",
    "refusal_outlived_by_link_change",
    "set_dive_calibration_target",
    "set_dive_slate_template",
    "set_species_needs_reprocess",
    "slate_templates_by_name",
    "species_grouping_facts",
    "species_population_facts",
    "species_preprocess_facts",
    "supersede_species_labels",
]

#: A refused calibration row `r` of dive `d` that the dive's last link change
#: has outlived. The link setters below (the species sync's, and an admin's
#: through the portal) stamp `calibration_links_changed_at` with every write,
#: as v1 cleared the refusal with every write; both sides are the database's
#: clock. The calibration
#: cohorts read a dive's current refused row as standing only while this is
#: false (and while no label is newer than its `inputs_as_of`).
REFUSAL_OUTLIVED_SQL = (
    "(d.calibration_links_changed_at IS NOT NULL "
    "AND d.calibration_links_changed_at >= r.created_at)"
)

#: The canonical capture `c` has a live species label in a Label Studio
#: project: populate has seeded (or a labeler holds) its task. Sentinels and
#: superseded rows don't count.
_HAS_LIVE_SPECIES_TASK = """
    EXISTS (
        SELECT 1 FROM species_labels s
        WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
          AND s.ls_project_id IS NOT NULL AND NOT s.superseded
    )
"""

_LABEL_COLUMNS = """
    s.id, s.number, s.capture_id, s.ls_project_id, s.ls_task_id, s.completed,
    s.superseded, s.needs_reprocess, s.grouping, s.top_three_photos_of_group,
    s.content_of_image, s.fish_measurable_category, s.fish_angle_category,
    s.fish_curved_category
"""


@dataclass(frozen=True)
class SpeciesCandidate:
    dive_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class SpeciesCapture:
    capture_id: uuid.UUID
    #: v1's image id for a migrated capture.
    number: int
    checksum: str
    #: Migrated from v1: its JPEG may still be where v1 wrote it.
    from_v1: bool
    captured_at: datetime


@dataclass(frozen=True)
class SpeciesLabelRow:
    """A live species label, as the workers read one."""

    id: uuid.UUID
    number: int
    capture_id: uuid.UUID
    #: None: a sentinel (an imported judgement, not a labeler's task).
    ls_project_id: int | None
    ls_task_id: int | None
    completed: bool
    superseded: bool
    needs_reprocess: bool
    grouping: str | None
    top_three_photos_of_group: bool | None
    content_of_image: str | None
    fish_measurable_category: str | None
    fish_angle_category: str | None
    fish_curved_category: str | None


@dataclass(frozen=True)
class CameraIntrinsicsRow:
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]


@dataclass(frozen=True)
class SpeciesPreprocessFacts:
    """Everything the stage-2 resolver reads about a dive (v1: six SDK calls)."""

    #: v1's `dive.camera_id`; None when the dive names no device.
    device_id: uuid.UUID | None
    #: The device's current camera calibration; None when it has none.
    intrinsics: CameraIntrinsicsRow | None
    #: The dive's canonical captures, in capture order.
    captures: list[SpeciesCapture]
    #: Capture ids per prediction cluster, clusters and members in order.
    prediction_clusters: list[list[uuid.UUID]]
    #: Captures of the dive carrying a valid laser label.
    valid_laser: frozenset[uuid.UUID]
    #: The live (not superseded) species labels of the dive's captures,
    #: sentinels included (v1's `get_species_labels`).
    species_labels: list[SpeciesLabelRow]


@dataclass(frozen=True)
class SpeciesPopulationFacts:
    """What populate selects from."""

    #: Canonical captures with a valid laser label, in capture order.
    candidates: list[SpeciesCapture]
    #: The live species labels of the dive's captures, sentinels included.
    species_labels: list[SpeciesLabelRow]


@dataclass(frozen=True)
class SpeciesGroupingFacts:
    """What stage 6.1 reads."""

    #: The dive already has label-studio clusters (v1 refuses to re-run).
    already_grouped: bool
    prediction_clusters: list[list[uuid.UUID]]
    species_labels: list[SpeciesLabelRow]


# -- the cohorts ------------------------------------------------------------------


async def next_dive_for_species_preprocessing(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> SpeciesCandidate | None:
    """The tenant's oldest dive in the stage-2 cohort."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND (
                      EXISTS (
                          SELECT 1 FROM captures c
                          JOIN laser_labels l
                            ON l.tenant_id = c.tenant_id AND l.capture_id = c.id
                          WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                            AND c.is_canonical AND {VALID_LASER}
                            AND NOT {_HAS_LIVE_SPECIES_TASK}
                            -- The qualifying capture must itself be clustered:
                            -- the resolver needs its cluster for "i of N".
                            AND EXISTS (
                                SELECT 1 FROM dive_frame_cluster_captures m
                                JOIN dive_frame_clusters k
                                  ON k.tenant_id = m.tenant_id
                                 AND k.id = m.cluster_id
                                WHERE m.tenant_id = c.tenant_id
                                  AND m.capture_id = c.id
                                  AND k.formed_by = 'prediction'
                            )
                      )
                      OR EXISTS (
                          SELECT 1 FROM captures c
                          JOIN species_labels s
                            ON s.tenant_id = c.tenant_id AND s.capture_id = c.id
                          WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                            AND c.is_canonical AND s.needs_reprocess
                            AND NOT s.superseded
                      )
                  )
                ORDER BY d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id},
        )
    ).one_or_none()
    return None if row is None else SpeciesCandidate(row.id, row.created_at)


async def dives_needing_species_population(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[SpeciesCandidate]:
    """Every dive of the tenant needing species tasks (re)populated, oldest
    first. No cluster gate and no flag: populate needs only the JPEG, which it
    checks itself."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.created_at FROM dives d
            WHERE d.tenant_id = :tenant AND d.priority = 'high'
              AND EXISTS (
                  SELECT 1 FROM captures c
                  JOIN laser_labels l
                    ON l.tenant_id = c.tenant_id AND l.capture_id = c.id
                  WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                    AND c.is_canonical AND {VALID_LASER}
                    AND NOT {_HAS_LIVE_SPECIES_TASK}
              )
            ORDER BY d.created_at, d.id
            """),
        {"tenant": tenant_id},
    )
    return [SpeciesCandidate(r.id, r.created_at) for r in rows]


# -- reads ------------------------------------------------------------------------


async def _canonical_captures(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, laser: bool
) -> list[SpeciesCapture]:
    valid = (
        f"""AND EXISTS (
                SELECT 1 FROM laser_labels l
                WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
                  AND {VALID_LASER}
            )"""
        if laser
        else ""
    )
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.number, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                   c.captured_at
            FROM captures c
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive AND c.is_canonical
              {valid}
            ORDER BY c.captured_at, c.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [
        SpeciesCapture(r.id, r.number, r.checksum, r.from_v1, r.captured_at)
        for r in rows
    ]


async def _prediction_clusters(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[list[uuid.UUID]]:
    rows = await conn.execute(
        text("""
            SELECT array_agg(c.id ORDER BY c.captured_at, c.number) AS members
            FROM dive_frame_clusters k
            JOIN dive_frame_cluster_captures m
              ON m.tenant_id = k.tenant_id AND m.cluster_id = k.id
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            WHERE k.tenant_id = :tenant AND k.dive_id = :dive
              AND k.formed_by = 'prediction'
            GROUP BY k.id
            ORDER BY min(c.captured_at), min(c.number), k.id
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [list(r.members) for r in rows]


async def _live_species_labels(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[SpeciesLabelRow]:
    rows = await conn.execute(
        text(f"""
            SELECT {_LABEL_COLUMNS} FROM species_labels s
            JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
            WHERE s.tenant_id = :tenant AND c.dive_id = :dive AND NOT s.superseded
            ORDER BY c.captured_at, c.number, s.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [SpeciesLabelRow(*r) for r in rows]


def _floats(value) -> list:
    value = json.loads(value) if isinstance(value, str) else value
    return [_floats(v) if isinstance(v, list) else float(v) for v in value]


async def species_preprocess_facts(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> SpeciesPreprocessFacts | None:
    """What the stage-2 resolver reads; None if the tenant has no such dive."""
    dive = (
        await conn.execute(
            text("SELECT device_id FROM dives WHERE tenant_id = :t AND id = :d"),
            {"t": tenant_id, "d": dive_id},
        )
    ).one_or_none()
    if dive is None:
        return None
    intrinsics = None
    if dive.device_id is not None:
        calibration = (
            await conn.execute(
                text("""
                    SELECT camera_matrix, distortion_coefficients
                    FROM current_camera_calibrations
                    WHERE tenant_id = :t AND device_id = :device
                    """),
                {"t": tenant_id, "device": dive.device_id},
            )
        ).one_or_none()
        if calibration is not None:
            intrinsics = CameraIntrinsicsRow(
                _floats(calibration.camera_matrix),
                _floats(calibration.distortion_coefficients),
            )
    valid = await conn.execute(
        text(f"""
            SELECT DISTINCT c.id FROM captures c
            JOIN laser_labels l ON l.tenant_id = c.tenant_id AND l.capture_id = c.id
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive AND {VALID_LASER}
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return SpeciesPreprocessFacts(
        device_id=dive.device_id,
        intrinsics=intrinsics,
        captures=await _canonical_captures(conn, tenant_id, dive_id, laser=False),
        prediction_clusters=await _prediction_clusters(conn, tenant_id, dive_id),
        valid_laser=frozenset(valid.scalars()),
        species_labels=await _live_species_labels(conn, tenant_id, dive_id),
    )


async def species_population_facts(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> SpeciesPopulationFacts:
    """What populate selects its targets from."""
    return SpeciesPopulationFacts(
        candidates=await _canonical_captures(conn, tenant_id, dive_id, laser=True),
        species_labels=await _live_species_labels(conn, tenant_id, dive_id),
    )


async def species_grouping_facts(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> SpeciesGroupingFacts:
    """What stage 6.1 reads."""
    grouped = (
        await conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM dive_frame_clusters
                    WHERE tenant_id = :tenant AND dive_id = :dive
                      AND formed_by = 'label_studio'
                )
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).scalar_one()
    return SpeciesGroupingFacts(
        already_grouped=grouped,
        prediction_clusters=await _prediction_clusters(conn, tenant_id, dive_id),
        species_labels=await _live_species_labels(conn, tenant_id, dive_id),
    )


# -- writes -----------------------------------------------------------------------


async def set_species_needs_reprocess(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    value: bool,
    *,
    only_incomplete: bool = True,
    capture_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Raise or lower `needs_reprocess` on the dive's species labels; the
    number of rows touched.

    Canonical captures only, both directions. Raising touches only live rows,
    by default only incomplete ones -- a flag on a row the resolver can't see
    would select a dive it finds no work for. Lowering touches every row, so a
    row completed or superseded after being flagged still comes down.
    `capture_ids` scopes to those frames; `[]` is an empty scope, not "all".
    """
    conditions = []
    if value:
        conditions.append("AND NOT s.superseded")
        if only_incomplete:
            conditions.append("AND NOT s.completed")
    if capture_ids is not None:
        conditions.append("AND c.id = ANY(:captures)")
    updated = await conn.execute(
        text(f"""
            UPDATE species_labels s SET needs_reprocess = :value
            FROM captures c
            WHERE s.tenant_id = :tenant AND c.tenant_id = s.tenant_id
              AND c.id = s.capture_id AND c.dive_id = :dive AND c.is_canonical
              {" ".join(conditions)}
            """),
        {
            "value": value,
            "tenant": tenant_id,
            "dive": dive_id,
            "captures": list(capture_ids or []),
        },
    )
    return updated.rowcount


async def record_species_label(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    capture_id: uuid.UUID,
    ls_project_id: int,
    ls_task_id: int,
    image_url: str,
) -> None:
    """Anchor the (capture, task, project) triple: populate's row for a task.

    v1's natural-key upsert on (image, project): a row the project already
    holds -- only a superseded one can reach here -- is re-anchored and
    revived with the fields v1's populate sent, and nothing else, so its
    `needs_reprocess` survives. `source` is `human`: a row seeded for a
    labeler (docs/port-plan.md).
    """
    await conn.execute(
        text("""
            INSERT INTO species_labels
                (tenant_id, capture_id, source, ls_project_id, ls_task_id,
                 image_url, completed, superseded, ls_payload)
            VALUES (:tenant, :capture, 'human', :project, :task, :url, false,
                    false, '{}'::jsonb)
            ON CONFLICT (tenant_id, capture_id, ls_project_id) DO UPDATE SET
                ls_task_id = excluded.ls_task_id,
                image_url = excluded.image_url,
                completed = false,
                superseded = false,
                ls_payload = '{}'::jsonb,
                ls_labeler_id = NULL,
                ls_updated_at = NULL,
                grouping = NULL,
                top_three_photos_of_group = NULL,
                content_of_image = NULL,
                fish_measurable_category = NULL,
                fish_angle_category = NULL,
                fish_curved_category = NULL
            """),
        {"tenant": tenant_id, "capture": capture_id, "project": ls_project_id,
         "task": ls_task_id, "url": image_url},
    )  # fmt: skip


async def supersede_species_labels(
    conn: AsyncConnection, tenant_id: uuid.UUID, label_ids: Sequence[uuid.UUID]
) -> int:
    """Dead-letter the named rows, if they are still open (incomplete and
    live); the number retired. A row completed since it was read is left be."""
    updated = await conn.execute(
        text("""
            UPDATE species_labels SET superseded = true
            WHERE tenant_id = :tenant AND id = ANY(:ids)
              AND NOT completed AND NOT superseded
            """),
        {"tenant": tenant_id, "ids": list(label_ids)},
    )
    return updated.rowcount


async def persist_label_studio_clusters(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    groups: list[list[uuid.UUID]],
) -> int | None:
    """Write stage 6.1's label-studio clusters, all or nothing, in the
    caller's transaction. The number written; None if the dive already has
    label-studio clusters (v1 refused to re-run: it had no delete)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"label-studio-clusters:{dive_id}"},
    )
    if (await species_grouping_facts(conn, tenant_id, dive_id)).already_grouped:
        return None

    groups = [group for group in groups if group]
    members = {capture for group in groups for capture in group}
    canonical = set(
        (
            await conn.execute(
                text("""
                    SELECT id FROM captures
                    WHERE tenant_id = :tenant AND dive_id = :dive
                      AND is_canonical AND id = ANY(:ids)
                    """),
                {"tenant": tenant_id, "dive": dive_id, "ids": list(members)},
            )
        ).scalars()
    )
    if foreign := members - canonical:
        raise ForeignCapture(
            f"not canonical captures of dive {dive_id}: {sorted(map(str, foreign))}"
        )
    for group in groups:
        if len(set(group)) != len(group):
            raise InvalidClusters(f"a capture repeats within a group of {dive_id}")
        cluster_id = (
            await conn.execute(
                text("""
                    INSERT INTO dive_frame_clusters
                        (tenant_id, dive_id, formed_by, updated_at)
                    VALUES (:tenant, :dive, 'label_studio', now())
                    RETURNING id
                    """),
                {"tenant": tenant_id, "dive": dive_id},
            )
        ).scalar_one()
        await conn.execute(
            text("""
                INSERT INTO dive_frame_cluster_captures
                    (tenant_id, cluster_id, capture_id)
                SELECT :tenant, :cluster, unnest(CAST(:captures AS uuid[]))
                """),
            {"tenant": tenant_id, "cluster": cluster_id, "captures": group},
        )
    return len(groups)


# -- the dive links (written by the species sync, and by an admin's portal routes) --


async def slate_templates_by_name(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    """Every slate template, by name (global reference data)."""
    rows = await conn.execute(text("SELECT name, id FROM slate_templates"))
    return {r.name: r.id for r in rows}


async def calibration_targets_by_name(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    """Every calibration target's current row, by name."""
    rows = await conn.execute(text("SELECT name, id FROM current_calibration_targets"))
    return {r.name: r.id for r in rows}


async def _set_link(conn, tenant_id, dive_id, column, value) -> bool:
    updated = await conn.execute(
        text(f"""
            UPDATE dives SET {column} = :value, calibration_links_changed_at = now()
            WHERE tenant_id = :tenant AND id = :dive
            """),
        {"value": value, "tenant": tenant_id, "dive": dive_id},
    )
    return updated.rowcount > 0


async def set_dive_slate_template(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    slate_template_id: uuid.UUID,
) -> bool:
    """v1's `set_dive_slate`: set the dive's slate template, and expire any
    standing calibration refusal. False if the tenant has no such dive."""
    return await _set_link(
        conn, tenant_id, dive_id, "slate_template_id", slate_template_id
    )


async def set_dive_calibration_target(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    calibration_target_id: uuid.UUID,
) -> bool:
    """v1's `set_dive_calibration_target`: set the dive's planar calibration
    target, and expire any standing refusal. False if there is no such dive."""
    return await _set_link(
        conn, tenant_id, dive_id, "calibration_target_id", calibration_target_id
    )


async def clear_dive_calibration_target(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> bool:
    """v1's `clear_dive_calibration_target`: unlink the dive from any board
    (idempotent), so it leaves the checkerboard cohort. Like v1's, it does not
    touch a refusal -- an unlinked dive has nothing more to fit from the board
    -- so it does not stamp the link change. False if there is no such dive."""
    updated = await conn.execute(
        text("""
            UPDATE dives SET calibration_target_id = NULL
            WHERE tenant_id = :tenant AND id = :dive
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return updated.rowcount > 0


async def note_unidentified_slate(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID, note: str
) -> bool:
    """Write `note` on a dive with no note; True if written. Never overwrites
    an operator's note, never touches priority (v1)."""
    updated = await conn.execute(
        text("""
            UPDATE dives SET notes = :note
            WHERE tenant_id = :tenant AND id = :dive
              AND (notes IS NULL OR notes = '')
            """),
        {"note": note, "tenant": tenant_id, "dive": dive_id},
    )
    return updated.rowcount > 0


async def refusal_outlived_by_link_change(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> bool:
    """Whether the dive's current calibration row is a refusal that a later
    link change has expired (`REFUSAL_OUTLIVED_SQL`)."""
    return bool(
        (
            await conn.execute(
                text(f"""
                    SELECT {REFUSAL_OUTLIVED_SQL}
                    FROM dives d
                    JOIN current_laser_calibrations r
                      ON r.tenant_id = d.tenant_id AND r.dive_id = d.id
                    WHERE d.tenant_id = :tenant AND d.id = :dive
                      AND r.outcome = 'refused'
                    """),
                {"tenant": tenant_id, "dive": dive_id},
            )
        ).scalar_one_or_none()
    )


class SpeciesCatalog(ServicePrincipal):
    """The species stages' database side, as the orchestrator's service
    principal."""

    async def next_dive_for_species_preprocessing(
        self, tenant_id: uuid.UUID
    ) -> SpeciesCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_species_preprocessing(conn, tenant_id)

    async def dives_needing_species_population(
        self, tenant_id: uuid.UUID
    ) -> list[SpeciesCandidate]:
        async with self._tenant(tenant_id) as conn:
            return await dives_needing_species_population(conn, tenant_id)

    async def species_preprocess_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPreprocessFacts | None:
        async with self._tenant(tenant_id) as conn:
            return await species_preprocess_facts(conn, tenant_id, dive_id)

    async def set_species_needs_reprocess(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        value: bool,
        *,
        only_incomplete: bool = True,
        capture_ids: Sequence[uuid.UUID] | None = None,
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await set_species_needs_reprocess(
                conn,
                tenant_id,
                dive_id,
                value,
                only_incomplete=only_incomplete,
                capture_ids=capture_ids,
            )

    async def species_population_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPopulationFacts:
        async with self._tenant(tenant_id) as conn:
            return await species_population_facts(conn, tenant_id, dive_id)

    async def record_species_label(
        self,
        tenant_id: uuid.UUID,
        *,
        capture_id: uuid.UUID,
        ls_project_id: int,
        ls_task_id: int,
        image_url: str,
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await record_species_label(
                conn,
                tenant_id,
                capture_id=capture_id,
                ls_project_id=ls_project_id,
                ls_task_id=ls_task_id,
                image_url=image_url,
            )

    async def supersede_species_labels(
        self, tenant_id: uuid.UUID, label_ids: Sequence[uuid.UUID]
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await supersede_species_labels(conn, tenant_id, label_ids)

    async def species_grouping_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesGroupingFacts:
        async with self._tenant(tenant_id) as conn:
            return await species_grouping_facts(conn, tenant_id, dive_id)

    async def persist_label_studio_clusters(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, groups: list[list[uuid.UUID]]
    ) -> int | None:
        async with self._tenant(tenant_id) as conn:
            return await persist_label_studio_clusters(conn, tenant_id, dive_id, groups)

    async def slate_templates_by_name(
        self, tenant_id: uuid.UUID
    ) -> dict[str, uuid.UUID]:
        async with self._tenant(tenant_id) as conn:
            return await slate_templates_by_name(conn)

    async def calibration_targets_by_name(
        self, tenant_id: uuid.UUID
    ) -> dict[str, uuid.UUID]:
        async with self._tenant(tenant_id) as conn:
            return await calibration_targets_by_name(conn)

    async def set_dive_slate_template(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await set_dive_slate_template(
                conn, tenant_id, dive_id, slate_template_id
            )

    async def set_dive_calibration_target(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        calibration_target_id: uuid.UUID,
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await set_dive_calibration_target(
                conn, tenant_id, dive_id, calibration_target_id
            )

    async def note_unidentified_slate(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, note: str
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await note_unidentified_slate(conn, tenant_id, dive_id, note)
