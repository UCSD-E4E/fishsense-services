"""The database side of stage 9 and the dive-slate label project, tenant-scoped.

Ported from fishsense-lite@77e8f8e5:

* the stage-9 cohort, `GET /api/v1/dives/select-next/slate-preprocessing/`
  (services/fishsense-api/src/fishsense_api/controllers/
  dive_cohort_controller.py, with `_has_image_flagged_for_reprocess`);
* its resolver, resolve_slate_preprocess_inputs_activity.py (an SDK walk over
  the dive, its intrinsics, the slate list, species and slate labels and
  images, filtered in Python -- a query here);
* the flag endpoints, label_reprocess_controller.py `_set_needs_reprocess`
  (the dive-slate kind), which clear_slate_reprocess_flags_activity called;
* populate_dive_slate_label_studio_project_activity.py's reads and writes
  (`_select_target_image_ids`, the `put_dive_slate_label` anchoring a task,
  the supersede pass, the publish check);
* the sync's reads and writes (label_controller.py's slate project ids and
  label-by-task lookup, and the PUT of sync_dive_slate_labels_for_label_
  studio_project_activity.py's `_update_slate_label`), and the image -> dive
  -> slate lookup its panel offset needs.

v1's rules, kept:

* the stage-9 cohort is HIGH + a slate template + a canonical frame whose
  species label says `Slate, Laser on slate` and which carries no live
  (non-superseded) slate label in a real project -- a NULL-project sentinel
  does not count -- OR a canonical frame whose live slate label is flagged
  `needs_reprocess`. The resolver mirrors it exactly, so a picked dive always
  has work: a flagged frame skips the marker gate, and a flag that resolves to
  nothing is lowered for the whole dive by the parent;
* raising a flag touches canonical, live, (by default) incomplete rows;
  clearing touches every canonical row, completed or not, scoped to the
  frames a run redrew (None: the whole dive; []: nothing);
* populate targets frames with no *completed* slate label, and its supersede
  pass spares a row only when it is in this project AND its frame was a
  candidate this run (prod dive 341 oscillated without both halves);
* the sync finds a label by its task among live rows and skips a task with
  none; it sets `completed` from the task, and geometry and skipped points
  only when the annotation carries them.

v2 changes:

* per tenant; candidates oldest first by `created_at`, so the orchestrator
  can take the oldest across the tenants it serves;
* **the cohort ignores a superseded species label**, as the resolver always
  did: v1's cohort read every species row while its resolver's getter dropped
  superseded ones, so a dead-lettered marker picked a dive that resolved
  nothing -- hourly, ahead of every newer dive;
* **the cohort offers only a dive stage 9 can run**: a camera calibration
  for its device, and a template with a dpi, reference points and a NAS
  path. v1 offered the rest and its resolver (or `stage_slate_pdf`) raised
  every hour, ahead of every newer dive -- across tenants, every tenant's.
  Each is fixed in reference data, so the dive simply comes back then;
* **populate targets canonical frames only.** v1 took every marked image; a
  duplicate shares its canonical twin's checksum, hence its JPEG and task URL,
  and v2 holds one label per Label Studio task;
* recording a task **re-anchors** the row (task, URL, revived) rather than
  replacing it: v1 PUT a fresh row, wiping geometry a sync had written and
  whose cursor would not re-send it. The reprocess flag stays, as in v1;
* the sync writes only the columns it owns (never `needs_reprocess` or
  `superseded`), as the laser sync's port does;
* the camera is the dive's device's current camera calibration (v1: the
  camera's intrinsics).
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
from fishsense_services_api.taxonomy_sql import SLATE_CONTENT_MARKER

__all__ = [
    "SlateCatalog",
    "SlateInputsUnavailable",
    "SlatePopulateCapture",
    "SlatePreprocessCandidate",
    "SlatePreprocessCapture",
    "SlatePreprocessInputs",
    "SlateSync",
    "SlateTaskTemplate",
    "SlateTemplate",
    "apply_slate_sync",
    "clear_slate_reprocess_flags",
    "dive_has_slate_labels_in_project",
    "flag_slate_labels_for_reprocess",
    "next_dive_for_slate_preprocessing",
    "record_slate_label",
    "slate_label_studio_projects",
    "slate_populate_candidates",
    "slate_preprocess_cohort",
    "slate_preprocess_inputs",
    "slate_preprocess_work",
    "slate_template",
    "slate_template_for_task",
    "supersede_stale_slate_labels",
]


class SlateInputsUnavailable(ValueError):
    """A dive stage 9 cannot be resolved for: the reason is in the message."""


@dataclass(frozen=True)
class SlatePreprocessCandidate:
    dive_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class SlateTemplate:
    id: uuid.UUID
    name: str
    dpi: int | None
    #: The template's reference points, in PDF pixels at `dpi`.
    reference_points: list[tuple[float, float]]
    #: Share-relative NAS path of the template PDF (v1's `DiveSlate.path`).
    source_path: str | None
    #: v1's id for a migrated template: v1 staged its PDF under it.
    v1_id: int | None = None


@dataclass(frozen=True)
class SlatePreprocessCapture:
    capture_id: uuid.UUID
    checksum: str
    #: Migrated from v1: its JPEG may still be where v1 wrote it.
    from_v1: bool


@dataclass(frozen=True)
class SlatePreprocessInputs:
    dive_id: uuid.UUID
    slate_template: SlateTemplate
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    captures: list[SlatePreprocessCapture]


@dataclass(frozen=True)
class SlatePopulateCapture:
    capture_id: uuid.UUID
    #: v1's image id for a migrated capture: the task's `image_id`.
    number: int
    checksum: str
    from_v1: bool
    captured_at: datetime | None


@dataclass(frozen=True)
class SlateSync:
    """What one Label Studio task says about its slate label, in photo pixels
    (the orchestrator has removed the composite's panel offset)."""

    completed: bool
    #: None: the annotation held none, so the last ones are kept (v1).
    reference_points: list[tuple[float, float]] | None
    slate_rectangle: list[tuple[float, float]] | None
    #: 0-based (Label Studio shows them 1-based).
    skipped_points: list[int] | None
    #: The most recent annotator's Label Studio user id; None keeps the last.
    ls_labeler_id: int | None
    ls_updated_at: datetime | None
    ls_payload: dict[str, Any]


@dataclass(frozen=True)
class SlateTaskTemplate:
    """The live slate label a task anchors, and its dive's slate template."""

    capture_id: uuid.UUID
    slate_template_id: uuid.UUID | None


#: A capture of dive `d` (aliased) that is its canonical copy.
_CANONICAL_OF_DIVE = """
    c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
"""


def _marked_and_unlabeled(marker: str) -> str:
    """A frame still to preprocess: a live slate marker, and no live slate
    label in a real project. `c` is the capture; `marker` is SQL for the
    stage-9 marker (a bind parameter, or a literal in a view)."""
    return f"""
    EXISTS (
        SELECT 1 FROM species_labels sp
        WHERE sp.tenant_id = c.tenant_id AND sp.capture_id = c.id
          AND sp.content_of_image = {marker} AND NOT sp.superseded
    )
    AND NOT EXISTS (
        SELECT 1 FROM slate_labels s
        WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
          AND s.ls_project_id IS NOT NULL AND NOT s.superseded
    )
"""


_MARKED_AND_UNLABELED = _marked_and_unlabeled(":marker")

#: Dive `d` is one stage 9 can run at all: its device has a current camera
#: calibration, and its template can be scaled (a dpi, reference points) and
#: staged (a NAS path). The resolver and `stage_slate_pdf` refuse the rest.
_RESOLVABLE = """
    EXISTS (
        SELECT 1 FROM current_camera_calibrations cc
        WHERE cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
    )
    AND EXISTS (
        SELECT 1 FROM slate_templates st
        WHERE st.id = d.slate_template_id AND st.dpi IS NOT NULL
          AND jsonb_typeof(st.reference_points) = 'array'
          AND st.reference_points <> '[]'::jsonb
          AND coalesce(st.source_path, '') <> ''
    )
"""

#: A frame flagged for a redraw on a live slate label. `c` is the capture.
_FLAGGED = """
    EXISTS (
        SELECT 1 FROM slate_labels s
        WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
          AND s.needs_reprocess AND NOT s.superseded
    )
"""


# --- stage 9: the cohort and the resolver -------------------------------------


def slate_preprocess_work(marker: str) -> str:
    """Dive `d` has stage-9 work: a canonical frame marked and unlabeled, or
    flagged for a redraw. `marker` is SQL for the stage-9 marker. Named, with
    the cohort, so `dive_pipeline_status` reads the same predicates
    (migration pipeline_status_01)."""
    return f"""EXISTS (
        SELECT 1 FROM captures c
        WHERE {_CANONICAL_OF_DIVE}
          AND (({_marked_and_unlabeled(marker)}) OR {_FLAGGED})
    )"""


def slate_preprocess_cohort(marker: str) -> str:
    """The stage-9 cohort over dive `d`, but for the tenant and priority terms
    the selector adds: a slate template, a dive stage 9 can resolve, and
    work."""
    return (
        f"d.slate_template_id IS NOT NULL AND {_RESOLVABLE}"
        f" AND {slate_preprocess_work(marker)}"
    )


async def next_dive_for_slate_preprocessing(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> SlatePreprocessCandidate | None:
    """The tenant's oldest dive in the stage-9 cohort."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND {slate_preprocess_cohort(":marker")}
                ORDER BY d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id, "marker": SLATE_CONTENT_MARKER},
        )
    ).one_or_none()
    return None if row is None else SlatePreprocessCandidate(row.id, row.created_at)


def _template(row) -> SlateTemplate:
    return SlateTemplate(
        id=row.id,
        name=row.name,
        dpi=row.dpi,
        reference_points=[tuple(p) for p in (row.reference_points or [])],
        source_path=row.source_path,
        v1_id=row.v1_id,
    )


async def slate_template(
    conn: AsyncConnection, slate_template_id: uuid.UUID
) -> SlateTemplate | None:
    """A slate template (global reference data)."""
    row = (
        await conn.execute(
            text("""
                SELECT id, name, dpi, reference_points, source_path, v1_id
                FROM slate_templates WHERE id = :id
                """),
            {"id": slate_template_id},
        )
    ).one_or_none()
    return None if row is None else _template(row)


async def _dive_setup(conn, tenant_id, dive_id):
    """The dive's slate template and its device's current camera calibration."""
    row = (
        await conn.execute(
            text("""
                SELECT d.slate_template_id, cc.id AS calibration_id,
                       cc.camera_matrix, cc.distortion_coefficients
                FROM dives d
                LEFT JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                WHERE d.tenant_id = :tenant AND d.id = :dive
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).one_or_none()
    if row is None:
        raise SlateInputsUnavailable(f"dive_id={dive_id} not found")
    return row


async def slate_preprocess_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> SlatePreprocessInputs:
    """Everything stage 9 needs for the dive: its template, its camera, and
    the canonical frames to (re)draw -- the cohort's two branches, marked
    frames first, then flagged ones, each once, in capture order."""
    setup = await _dive_setup(conn, tenant_id, dive_id)
    if setup.slate_template_id is None:
        raise SlateInputsUnavailable(f"dive_id={dive_id} has no slate template")
    if setup.calibration_id is None:
        raise SlateInputsUnavailable(
            f"dive_id={dive_id} has no camera calibration for its device"
        )
    template = await slate_template(conn, setup.slate_template_id)
    if template is None:
        raise SlateInputsUnavailable(
            f"slate_template_id={setup.slate_template_id} not found"
        )
    if template.dpi is None or not template.reference_points:
        raise SlateInputsUnavailable(
            f"slate_template_id={template.id} missing dpi or reference_points"
        )

    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                   ({_MARKED_AND_UNLABELED}) AS marked
            FROM captures c JOIN dives d ON d.tenant_id = c.tenant_id
            WHERE d.tenant_id = :tenant AND d.id = :dive AND {_CANONICAL_OF_DIVE}
              AND (({_MARKED_AND_UNLABELED}) OR {_FLAGGED})
            ORDER BY NOT ({_MARKED_AND_UNLABELED}), c.number, c.id
            """),
        {"tenant": tenant_id, "dive": dive_id, "marker": SLATE_CONTENT_MARKER},
    )
    return SlatePreprocessInputs(
        dive_id=dive_id,
        slate_template=template,
        camera_calibration_id=setup.calibration_id,
        camera_matrix=setup.camera_matrix,
        distortion_coefficients=setup.distortion_coefficients,
        captures=[SlatePreprocessCapture(r.id, r.checksum, r.from_v1) for r in rows],
    )


# --- the reprocess flags ------------------------------------------------------


async def flag_slate_labels_for_reprocess(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    only_incomplete: bool = True,
) -> int:
    """Raise `needs_reprocess` on the dive's canonical, live slate labels
    (incomplete ones only, by default: an answered frame needs no redraw)."""
    result = await conn.execute(
        text("""
            UPDATE slate_labels s SET needs_reprocess = true
            FROM captures c
            WHERE s.tenant_id = :tenant AND c.tenant_id = s.tenant_id
              AND c.id = s.capture_id AND c.dive_id = :dive AND c.is_canonical
              AND NOT s.superseded
              AND (NOT :only_incomplete OR NOT s.completed)
            """),
        {"tenant": tenant_id, "dive": dive_id, "only_incomplete": only_incomplete},
    )
    return result.rowcount


async def clear_slate_reprocess_flags(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    checksums: list[str] | None,
) -> int:
    """Lower `needs_reprocess` on the dive's canonical slate labels, whatever
    their state. `checksums` scopes it to the frames a run redrew; None is the
    whole dive (the no-work backstop); [] is nothing."""
    result = await conn.execute(
        text("""
            UPDATE slate_labels s SET needs_reprocess = false
            FROM captures c
            WHERE s.tenant_id = :tenant AND c.tenant_id = s.tenant_id
              AND c.id = s.capture_id AND c.dive_id = :dive AND c.is_canonical
              AND (CAST(:checksums AS text[]) IS NULL
                   OR c.checksum = ANY(CAST(:checksums AS text[])))
            """),
        {"tenant": tenant_id, "dive": dive_id, "checksums": checksums},
    )
    return result.rowcount


# --- populate -----------------------------------------------------------------


async def slate_populate_candidates(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[SlatePopulateCapture]:
    """The dive's canonical frames marked as slate (a live species label) with
    no completed live slate label, in capture order."""
    rows = await conn.execute(
        text("""
            SELECT c.id, c.number, c.checksum, c.v1_id IS NOT NULL AS from_v1,
                   c.captured_at
            FROM captures c
            WHERE c.tenant_id = :tenant AND c.dive_id = :dive AND c.is_canonical
              AND EXISTS (
                  SELECT 1 FROM species_labels sp
                  WHERE sp.tenant_id = c.tenant_id AND sp.capture_id = c.id
                    AND sp.content_of_image = :marker AND NOT sp.superseded
              )
              AND NOT EXISTS (
                  SELECT 1 FROM slate_labels s
                  WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id
                    AND s.completed AND NOT s.superseded
              )
            ORDER BY c.captured_at, c.number
            """),
        {"tenant": tenant_id, "dive": dive_id, "marker": SLATE_CONTENT_MARKER},
    )
    return [
        SlatePopulateCapture(r.id, r.number, r.checksum, r.from_v1, r.captured_at)
        for r in rows
    ]


async def record_slate_label(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    *,
    ls_project_id: int,
    ls_task_id: int,
    image_url: str,
) -> None:
    """Anchor the (capture, task, project) triple: a new row seeded for a
    labeler (`human`), or the existing one re-anchored and revived."""
    await conn.execute(
        text("""
            INSERT INTO slate_labels
                (tenant_id, capture_id, source, ls_project_id, ls_task_id,
                 image_url, completed, superseded)
            VALUES (:tenant, :capture, 'human', :project, :task, :url, false, false)
            ON CONFLICT (tenant_id, capture_id, ls_project_id) DO UPDATE SET
                ls_task_id = excluded.ls_task_id,
                image_url = excluded.image_url,
                superseded = false
            """),
        {"tenant": tenant_id, "capture": capture_id, "project": ls_project_id,
         "task": ls_task_id, "url": image_url},
    )  # fmt: skip


async def supersede_stale_slate_labels(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    *,
    ls_project_id: int,
    keep_capture_ids: list[uuid.UUID],
) -> int:
    """Dead-letter the dive's incomplete live slate labels this project no
    longer owns: rows in another project, and rows in this one whose frame
    was not a candidate this run. Returns how many."""
    result = await conn.execute(
        text("""
            UPDATE slate_labels s SET superseded = true
            FROM captures c
            WHERE s.tenant_id = :tenant AND c.tenant_id = s.tenant_id
              AND c.id = s.capture_id AND c.dive_id = :dive
              AND NOT s.completed AND NOT s.superseded
              AND NOT (s.ls_project_id IS NOT DISTINCT FROM :project
                       AND s.capture_id = ANY(CAST(:keep AS uuid[])))
            """),
        {"tenant": tenant_id, "dive": dive_id, "project": ls_project_id,
         "keep": list(keep_capture_ids)},
    )  # fmt: skip
    return result.rowcount


async def dive_has_slate_labels_in_project(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID, ls_project_id: int
) -> bool:
    """Whether the project already holds live slate labels of the dive."""
    return (
        await conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM slate_labels s JOIN captures c
                      ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
                    WHERE s.tenant_id = :tenant AND c.dive_id = :dive
                      AND s.ls_project_id = :project AND NOT s.superseded
                )
                """),
            {"tenant": tenant_id, "dive": dive_id, "project": ls_project_id},
        )
    ).scalar_one()


# --- the sync -----------------------------------------------------------------


async def slate_label_studio_projects(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[int]:
    """The tenant's Label Studio projects holding live slate labels."""
    rows = await conn.execute(
        text("""
            SELECT DISTINCT ls_project_id FROM slate_labels
            WHERE tenant_id = :tenant AND ls_project_id IS NOT NULL
              AND NOT superseded
            ORDER BY ls_project_id
            """),
        {"tenant": tenant_id},
    )
    return list(rows.scalars())


async def slate_template_for_task(
    conn: AsyncConnection, tenant_id: uuid.UUID, ls_task_id: int
) -> SlateTaskTemplate | None:
    """The live slate label a task anchors, and its dive's slate template (None
    when the dive has none); None when no live label anchors the task."""
    row = (
        await conn.execute(
            text("""
                SELECT s.capture_id, d.slate_template_id
                FROM slate_labels s
                JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
                LEFT JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
                WHERE s.tenant_id = :tenant AND s.ls_task_id = :task
                  AND NOT s.superseded
                """),
            {"tenant": tenant_id, "task": ls_task_id},
        )
    ).one_or_none()
    return (
        None
        if row is None
        else SlateTaskTemplate(row.capture_id, row.slate_template_id)
    )


def _json(value) -> str | None:
    return None if value is None else json.dumps([list(v) for v in value])


async def apply_slate_sync(
    conn: AsyncConnection, tenant_id: uuid.UUID, ls_task_id: int, sync: SlateSync
) -> bool:
    """Update the tenant's live slate label for this task. False when none."""
    updated = await conn.execute(
        text("""
            UPDATE slate_labels SET
                completed = :completed,
                reference_points = coalesce(CAST(:refs AS jsonb), reference_points),
                slate_rectangle = coalesce(CAST(:rect AS jsonb), slate_rectangle),
                skipped_points = coalesce(CAST(:skip AS jsonb), skipped_points),
                ls_labeler_id = coalesce(:labeler, ls_labeler_id),
                ls_updated_at = :updated_at,
                ls_payload = CAST(:payload AS jsonb)
            WHERE tenant_id = :tenant AND ls_task_id = :task AND NOT superseded
            """),
        {
            "completed": sync.completed,
            "refs": _json(sync.reference_points),
            "rect": _json(sync.slate_rectangle),
            "skip": (
                None
                if sync.skipped_points is None
                else json.dumps(list(sync.skipped_points))
            ),
            "labeler": sync.ls_labeler_id,
            "updated_at": sync.ls_updated_at,
            "payload": json.dumps(sync.ls_payload),
            "tenant": tenant_id,
            "task": ls_task_id,
        },
    )
    return updated.rowcount > 0


class SlateCatalog(ServicePrincipal):
    """Stage 9 and the dive-slate project's database side, as the
    orchestrator's service principal."""

    async def next_dive_for_slate_preprocessing(
        self, tenant_id: uuid.UUID
    ) -> SlatePreprocessCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_slate_preprocessing(conn, tenant_id)

    async def slate_preprocess_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SlatePreprocessInputs:
        async with self._tenant(tenant_id) as conn:
            return await slate_preprocess_inputs(conn, tenant_id, dive_id)

    async def slate_template(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> SlateTemplate | None:
        async with self._tenant(tenant_id) as conn:
            return await slate_template(conn, slate_template_id)

    async def clear_slate_reprocess_flags(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, checksums: list[str] | None
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await clear_slate_reprocess_flags(
                conn, tenant_id, dive_id, checksums=checksums
            )

    async def flag_slate_labels_for_reprocess(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, only_incomplete: bool = True
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await flag_slate_labels_for_reprocess(
                conn, tenant_id, dive_id, only_incomplete=only_incomplete
            )

    async def slate_populate_candidates(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[SlatePopulateCapture]:
        async with self._tenant(tenant_id) as conn:
            return await slate_populate_candidates(conn, tenant_id, dive_id)

    async def record_slate_label(
        self,
        tenant_id: uuid.UUID,
        capture_id: uuid.UUID,
        *,
        ls_project_id: int,
        ls_task_id: int,
        image_url: str,
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await record_slate_label(
                conn,
                tenant_id,
                capture_id,
                ls_project_id=ls_project_id,
                ls_task_id=ls_task_id,
                image_url=image_url,
            )

    async def supersede_stale_slate_labels(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        *,
        ls_project_id: int,
        keep_capture_ids: list[uuid.UUID],
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await supersede_stale_slate_labels(
                conn,
                tenant_id,
                dive_id,
                ls_project_id=ls_project_id,
                keep_capture_ids=keep_capture_ids,
            )

    async def dive_has_slate_labels_in_project(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, ls_project_id: int
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await dive_has_slate_labels_in_project(
                conn, tenant_id, dive_id, ls_project_id
            )

    async def slate_label_studio_projects(self, tenant_id: uuid.UUID) -> list[int]:
        async with self._tenant(tenant_id) as conn:
            return await slate_label_studio_projects(conn, tenant_id)

    async def slate_template_for_task(
        self, tenant_id: uuid.UUID, ls_task_id: int
    ) -> SlateTaskTemplate | None:
        async with self._tenant(tenant_id) as conn:
            return await slate_template_for_task(conn, tenant_id, ls_task_id)

    async def apply_slate_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: SlateSync
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await apply_slate_sync(conn, tenant_id, ls_task_id, sync)
