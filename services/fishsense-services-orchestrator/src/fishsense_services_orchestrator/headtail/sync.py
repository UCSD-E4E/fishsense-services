"""The head/tail label sync's activities.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(get_headtail_label_studio_project_ids_activity.py,
sync_headtail_labels_for_label_studio_project_activity.py). Behaviour is v1's:
the projects are those of live head/tail labels; each task newer than the
cursor is applied, **only when it has annotations** (unlike the laser sync, v1
wrote nothing for a task without); a task with no live label is skipped; the
cursor is per (kind, project) and moves only when every task succeeded.

v2 changes (the laser sync's): projects are listed across every tenant the
orchestrator serves; a task is applied through the catalog, which writes only
the columns the sync owns; the annotator is its Label Studio id, so v1's user
lookup (which 422'd on hosted Label Studio's dict annotators) is gone; the
cursor kind is `head_tail`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import List, Protocol

from temporalio import activity

from fishsense_services_api.label_sync_store import HeadTailSync
from fishsense_services_orchestrator.headtail.labeling import head_tail_sync_from_task
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import (
    LabelProject,
    sync_label_studio_project,
)

__all__ = ["HEAD_TAIL_SYNC_KIND", "HeadTailSyncActivities", "HeadTailSyncCatalog"]

#: The sync-cursor kind (v1's `headtail`, which migrate-v1 renames).
HEAD_TAIL_SYNC_KIND = "head_tail"


class HeadTailSyncCatalog(Protocol):
    """See ``fishsense_services_api.label_sync_store.LabelSyncCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def label_studio_projects(
        self, tenant_id: uuid.UUID, kind: str
    ) -> list[int]: ...

    async def apply_head_tail_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: HeadTailSync
    ) -> bool: ...

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


class HeadTailSyncActivities:
    def __init__(
        self, *, catalog: HeadTailSyncCatalog, label_studio_factory: Callable
    ) -> None:
        self._catalog = catalog
        self._label_studio_factory = label_studio_factory

    @activity.defn(name="head_tail_label_projects")
    async def head_tail_label_projects(self) -> List[LabelProject]:
        """Every served tenant's Label Studio projects holding live head/tail
        labels."""
        projects = [
            LabelProject(tenant_id, project_id)
            for tenant_id in await self._catalog.member_tenants()
            for project_id in await self._catalog.label_studio_projects(
                tenant_id, HEAD_TAIL_SYNC_KIND
            )
        ]
        activity.logger.info("found %d head/tail label projects", len(projects))
        return projects

    @activity.defn(name="sync_head_tail_labels")
    async def sync_head_tail_labels(self, project: LabelProject) -> None:
        """Sync one project's head/tail labels in from Label Studio."""
        skipped = unannotated = 0

        async def apply(task: LabelStudioTask) -> None:
            nonlocal skipped, unannotated
            if not task.annotations:
                unannotated += 1
                return
            if not await self._catalog.apply_head_tail_sync(
                project.tenant_id, task.id, head_tail_sync_from_task(task)
            ):
                skipped += 1

        await sync_label_studio_project(
            project,
            HEAD_TAIL_SYNC_KIND,
            ls=self._label_studio_factory(),
            catalog=self._catalog,
            apply=apply,
        )
        activity.logger.info(
            "head/tail sync project_id=%d skipped %d task(s) with no label and "
            "%d with no annotations",
            project.ls_project_id,
            skipped,
            unannotated,
        )
