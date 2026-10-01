"""Syncing one Label Studio project's labels in, against its cursor.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/utils.py (`sync_label_studio_project`)
and sync_laser_labels_for_label_studio_project_activity.py
(`__update_laser_label`). Behaviour is v1's:

* the cursor (per kind and project) skips tasks whose `updated_at` is at or
  before it; a task with no readable timestamp is always processed;
* tasks are applied with bounded concurrency and a heartbeat per task;
* **the cursor moves only when every task succeeded**, to the newest
  `updated_at` seen. Replay is safe: applying a task is an update;
* a missing project is a no-op, so one rogue id doesn't fail the whole sync.

v2 changes: the project belongs to a tenant; reading a laser task is a pure
function (`laser_sync_from_task`) rather than a fetch-mutate-PUT.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from temporalio import activity

from fishsense_services_api.label_sync_store import LaserSync
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask

__all__ = [
    "LASER_LABEL_KEY_NAMES",
    "SYNC_CONCURRENCY",
    "LabelProject",
    "laser_sync_from_task",
    "sync_label_studio_project",
]

SYNC_CONCURRENCY = 8

#: The keypoint control's name in the laser label config; both have been used.
LASER_LABEL_KEY_NAMES = ["kp-1", "laser"]


@dataclass(frozen=True)
class LabelProject:
    tenant_id: uuid.UUID
    ls_project_id: int


class _LabelStudio(Protocol):
    async def project_exists(self, project_id: int) -> bool: ...

    async def list_tasks(self, project_id: int) -> list[LabelStudioTask]: ...


class _Cursors(Protocol):
    async def sync_cursor(
        self, tenant_id: uuid.UUID, kind: str, ls_project_id: int
    ) -> datetime | None: ...

    async def advance_sync_cursor(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        ls_project_id: int,
        last_synced_at: datetime,
    ) -> None: ...


async def sync_label_studio_project(
    project: LabelProject,
    kind: str,
    *,
    ls: _LabelStudio,
    catalog: _Cursors,
    apply: Callable[[LabelStudioTask], Awaitable[None]],
    concurrency: int = SYNC_CONCURRENCY,
) -> None:
    """Apply every task newer than the cursor, then advance it."""
    project_id = project.ls_project_id
    activity.logger.info(
        "label sync starting kind=%s tenant=%s project_id=%d",
        kind,
        project.tenant_id,
        project_id,
    )
    if not await ls.project_exists(project_id):
        return

    tasks = await ls.list_tasks(project_id)
    cursor = await catalog.sync_cursor(project.tenant_id, kind, project_id)

    eligible: list[LabelStudioTask] = []
    max_seen = cursor
    for task in tasks:
        ts = task.updated_at
        if cursor is not None and ts is not None and ts <= cursor:
            continue
        eligible.append(task)
        if ts is not None and (max_seen is None or ts > max_seen):
            max_seen = ts

    if not eligible:
        activity.logger.info(
            "label sync: no new tasks kind=%s project_id=%d total=%d cursor=%s",
            kind,
            project_id,
            len(tasks),
            cursor,
        )
        return
    activity.logger.info(
        "label sync running kind=%s project_id=%d total=%d eligible=%d cursor=%s",
        kind,
        project_id,
        len(tasks),
        len(eligible),
        cursor,
    )

    sem = asyncio.Semaphore(concurrency)

    async def _run(task: LabelStudioTask) -> None:
        async with sem:
            await apply(task)
            activity.heartbeat()

    # All or nothing: any failure raises out of the group, before the cursor.
    async with asyncio.TaskGroup() as group:
        for task in eligible:
            group.create_task(_run(task))

    if max_seen is not None and max_seen != cursor:
        await catalog.advance_sync_cursor(project.tenant_id, kind, project_id, max_seen)
        activity.logger.info(
            "label sync cursor advanced kind=%s project_id=%d cursor=%s",
            kind,
            project_id,
            max_seen,
        )


def laser_sync_from_task(task: LabelStudioTask) -> LaserSync:
    """What a Label Studio task says about its laser label.

    The keypoint comes from the first annotation's result under a laser
    control name, converted from Label Studio's percentages to pixels. With no
    keypoint, x/y/label are None and the store keeps the last ones (v1).
    """
    x = y = label = None
    if task.annotations:
        result = task.annotations[0].get("result", [])
        for key in LASER_LABEL_KEY_NAMES:
            section = next((r for r in result if r.get("from_name") == key), None)
            if section is not None:
                value = section["value"]
                x = value["x"] * section["original_width"] / 100
                y = value["y"] * section["original_height"] / 100
                label = value["keypointlabels"][0]
                break
    return LaserSync(
        completed=task.is_labeled,
        x=x,
        y=y,
        label=label,
        ls_labeler_id=task.annotator_id,
        ls_updated_at=task.updated_at,
        ls_payload=task.payload,
    )
