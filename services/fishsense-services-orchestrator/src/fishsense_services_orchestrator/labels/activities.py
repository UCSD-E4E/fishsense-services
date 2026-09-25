"""The laser-label sync's activities.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(get_laser_label_studio_project_ids_activity.py,
sync_laser_labels_for_label_studio_project_activity.py). v2 changes: projects
are listed across every tenant the orchestrator serves, each with its tenant,
and each task is applied through the label-sync catalog, which writes only the
columns the sync owns.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import List, Protocol

from temporalio import activity

from fishsense_services_api.label_sync_store import LaserSync
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import (
    LabelProject,
    laser_sync_from_task,
    sync_label_studio_project,
)

__all__ = ["LabelSyncActivities", "LabelSyncCatalog"]


class LabelSyncCatalog(Protocol):
    """See ``fishsense_services_api.label_sync_store.LabelSyncCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def label_studio_projects(
        self, tenant_id: uuid.UUID, kind: str
    ) -> list[int]: ...

    async def apply_laser_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: LaserSync
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


class LabelSyncActivities:
    def __init__(
        self, *, catalog: LabelSyncCatalog, label_studio_factory: Callable
    ) -> None:
        self._catalog = catalog
        self._label_studio_factory = label_studio_factory

    @activity.defn(name="laser_label_projects")
    async def laser_label_projects(self) -> List[LabelProject]:
        """Every served tenant's Label Studio projects holding live laser labels."""
        projects = [
            LabelProject(tenant_id, project_id)
            for tenant_id in await self._catalog.member_tenants()
            for project_id in await self._catalog.label_studio_projects(
                tenant_id, "laser"
            )
        ]
        activity.logger.info("found %d laser label projects", len(projects))
        return projects

    @activity.defn(name="sync_laser_labels")
    async def sync_laser_labels(self, project: LabelProject) -> None:
        """Sync one project's laser labels in from Label Studio."""
        skipped = 0

        async def apply(task: LabelStudioTask) -> None:
            nonlocal skipped
            if not await self._catalog.apply_laser_sync(
                project.tenant_id, task.id, laser_sync_from_task(task)
            ):
                # No label for this task: labels are created when a project is
                # populated, and the sync only updates them.
                skipped += 1

        await sync_label_studio_project(
            project,
            "laser",
            ls=self._label_studio_factory(),
            catalog=self._catalog,
            apply=apply,
        )
        if skipped:
            activity.logger.info(
                "laser sync project_id=%d skipped %d task(s) with no label",
                project.ls_project_id,
                skipped,
            )
