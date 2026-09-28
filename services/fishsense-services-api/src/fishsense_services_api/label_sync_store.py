"""The database side of the Label Studio label sync, tenant-scoped.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api: the laser-label
project ids (label_controller.get_laser_label_studio_project_ids), the sync
cursor endpoints, and the laser-label PUT the hourly sync used. v1's semantics:
the projects are those of live (not superseded) labels; a label is found by its
Label Studio task, and a task with none is skipped; the cursor is per (kind,
project).

v2 changes:

* per tenant;
* **the sync writes only the columns it owns.** v1 read a label, changed a few
  fields and PUT the whole row back, so a `needs_reprocess` raised in between
  was silently cleared. Here the update names its columns, and
  `needs_reprocess` and `superseded` are not among them;
* **a cursor never moves backwards**, so overlapping runs finishing out of
  order can't rewind each other.

The head/tail sync (`apply_head_tail_sync`) is ported from
fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
sync_headtail_labels_for_label_studio_project_activity.py
(`__update_headtail_label`) and the API's head/tail label-studio-project-ids,
with the same v2 changes. Two v1 rules are its own: a label is found among
*live* rows only (v1 read it with `superseded == False`), and the points move
only when both keypoints are present.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "HeadTailSync",
    "LabelSyncCatalog",
    "LaserSync",
    "advance_sync_cursor",
    "apply_head_tail_sync",
    "apply_laser_sync",
    "label_studio_projects",
    "sync_cursor",
]

#: The label table for each kind the sync knows.
_TABLES = {"laser": "laser_labels", "head_tail": "head_tail_labels"}


@dataclass(frozen=True)
class LaserSync:
    """What one Label Studio task says about its laser label."""

    completed: bool
    #: The keypoint, in pixels. None when the task holds none: the last one is
    #: kept (v1), and `completed` is what every cohort reads.
    x: float | None
    y: float | None
    label: str | None
    #: The most recent annotator's Label Studio user id; None keeps the last.
    ls_labeler_id: int | None
    ls_updated_at: datetime | None
    ls_payload: dict[str, Any]


@dataclass(frozen=True)
class HeadTailSync:
    """What one annotated Label Studio task says about its head/tail label."""

    completed: bool
    #: Snout and Fork, in pixels -- all four, or all None when the annotation
    #: lacks either keypoint (then the last ones are kept, as in v1).
    head_x: float | None
    head_y: float | None
    tail_x: float | None
    tail_y: float | None
    #: The most recent annotator's Label Studio user id; None keeps the last.
    ls_labeler_id: int | None
    ls_updated_at: datetime | None
    ls_payload: dict[str, Any]


async def label_studio_projects(
    conn: AsyncConnection, tenant_id: uuid.UUID, kind: str
) -> list[int]:
    """The tenant's Label Studio projects holding live labels of `kind`."""
    rows = await conn.execute(
        text(f"""
            SELECT DISTINCT ls_project_id FROM {_TABLES[kind]}
            WHERE tenant_id = :tenant AND ls_project_id IS NOT NULL
              AND NOT superseded
            ORDER BY ls_project_id
            """),
        {"tenant": tenant_id},
    )
    return list(rows.scalars())


async def apply_laser_sync(
    conn: AsyncConnection, tenant_id: uuid.UUID, ls_task_id: int, sync: LaserSync
) -> bool:
    """Update the tenant's laser label for this task. False when there is none."""
    has_point = sync.x is not None and sync.y is not None
    updated = await conn.execute(
        text("""
            UPDATE laser_labels SET
                completed = :completed,
                x = CASE WHEN :has_point THEN :x ELSE x END,
                y = CASE WHEN :has_point THEN :y ELSE y END,
                label = CASE WHEN :has_point THEN :label ELSE label END,
                ls_labeler_id = COALESCE(:labeler, ls_labeler_id),
                ls_updated_at = :updated_at,
                ls_payload = CAST(:payload AS jsonb)
            WHERE tenant_id = :tenant AND ls_task_id = :task
            """),
        {
            "completed": sync.completed,
            "has_point": has_point,
            "x": sync.x,
            "y": sync.y,
            "label": sync.label,
            "labeler": sync.ls_labeler_id,
            "updated_at": sync.ls_updated_at,
            "payload": json.dumps(sync.ls_payload),
            "tenant": tenant_id,
            "task": ls_task_id,
        },
    )
    return updated.rowcount > 0


async def apply_head_tail_sync(
    conn: AsyncConnection, tenant_id: uuid.UUID, ls_task_id: int, sync: HeadTailSync
) -> bool:
    """Update the tenant's live head/tail label for this task, writing only
    the sync's columns. False when the task has no live label."""
    points = None not in (sync.head_x, sync.head_y, sync.tail_x, sync.tail_y)
    updated = await conn.execute(
        text("""
            UPDATE head_tail_labels SET
                completed = :completed,
                head_x = CASE WHEN :points THEN :head_x ELSE head_x END,
                head_y = CASE WHEN :points THEN :head_y ELSE head_y END,
                tail_x = CASE WHEN :points THEN :tail_x ELSE tail_x END,
                tail_y = CASE WHEN :points THEN :tail_y ELSE tail_y END,
                ls_labeler_id = COALESCE(:labeler, ls_labeler_id),
                ls_updated_at = :updated_at,
                ls_payload = CAST(:payload AS jsonb)
            WHERE tenant_id = :tenant AND ls_task_id = :task AND NOT superseded
            """),
        {
            "completed": sync.completed,
            "points": points,
            "head_x": sync.head_x,
            "head_y": sync.head_y,
            "tail_x": sync.tail_x,
            "tail_y": sync.tail_y,
            "labeler": sync.ls_labeler_id,
            "updated_at": sync.ls_updated_at,
            "payload": json.dumps(sync.ls_payload),
            "tenant": tenant_id,
            "task": ls_task_id,
        },
    )
    return updated.rowcount > 0


async def sync_cursor(
    conn: AsyncConnection, tenant_id: uuid.UUID, kind: str, ls_project_id: int
) -> datetime | None:
    """How far the (kind, project) sync has got; None if it never has."""
    return (
        await conn.execute(
            text("""
                SELECT last_synced_at FROM label_studio_sync_cursors
                WHERE tenant_id = :tenant AND kind = :kind
                  AND ls_project_id = :project
                """),
            {"tenant": tenant_id, "kind": kind, "project": ls_project_id},
        )
    ).scalar_one_or_none()


async def advance_sync_cursor(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    kind: str,
    ls_project_id: int,
    last_synced_at: datetime,
) -> None:
    """Move the cursor forward to `last_synced_at` -- never backwards."""
    await conn.execute(
        text("""
            INSERT INTO label_studio_sync_cursors
                (tenant_id, kind, ls_project_id, last_synced_at)
            VALUES (:tenant, :kind, :project, :at)
            ON CONFLICT (tenant_id, kind, ls_project_id) DO UPDATE SET
                last_synced_at = GREATEST(
                    label_studio_sync_cursors.last_synced_at, excluded.last_synced_at
                )
            """),
        {"tenant": tenant_id, "kind": kind, "project": ls_project_id,
         "at": last_synced_at},
    )  # fmt: skip


class LabelSyncCatalog(ServicePrincipal):
    """The label sync's database side, as the orchestrator's service principal."""

    async def label_studio_projects(self, tenant_id: uuid.UUID, kind: str) -> list[int]:
        async with self._tenant(tenant_id) as conn:
            return await label_studio_projects(conn, tenant_id, kind)

    async def apply_laser_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: LaserSync
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await apply_laser_sync(conn, tenant_id, ls_task_id, sync)

    async def apply_head_tail_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: HeadTailSync
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await apply_head_tail_sync(conn, tenant_id, ls_task_id, sync)

    async def sync_cursor(
        self, tenant_id: uuid.UUID, kind: str, ls_project_id: int
    ) -> datetime | None:
        async with self._tenant(tenant_id) as conn:
            return await sync_cursor(conn, tenant_id, kind, ls_project_id)

    async def advance_sync_cursor(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        ls_project_id: int,
        last_synced_at: datetime,
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await advance_sync_cursor(
                conn, tenant_id, kind, ls_project_id, last_synced_at
            )
