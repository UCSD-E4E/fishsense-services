"""The thumbnail stage (new in v2, 2026-10-20).

Its activities hold the tenant stores, as the render stage's do, to presign
each render's GET and its thumbnail's PUT and check what was stored. The
hourly backlog schedule exists only when `FISHSENSE_THUMBNAILS_ENABLED` is
true; the workflow is registered either way.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.thumbnails_store import ThumbnailsCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores
from fishsense_services_orchestrator.thumbnails.activities import (
    ThumbnailActivities,
)
from fishsense_services_orchestrator.thumbnails.workflow import (
    ThumbnailBacklogWorkflow,
)

__all__ = ["STAGE", "thumbnail_schedules"]


def thumbnail_schedules(settings: RenderSettings) -> list[ScheduledWorkflow]:
    """The backlog's schedule, or none while thumbnails run on demand only."""
    if not settings.thumbnails_enabled:
        return []
    return [
        ScheduledWorkflow(
            schedule_id="thumbnail-backlog",
            workflow=ThumbnailBacklogWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=42),
            run_timeout=timedelta(hours=23),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ]


def _activities(deps: Deps):
    activities = ThumbnailActivities(
        catalog=ThumbnailsCatalog(deps.engine, sub=deps.sub),
        stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
    )
    return [
        activities.select_thumbnail_tenants,
        activities.resolve_thumbnails,
        activities.persist_thumbnails,
    ]


STAGE = Stage(
    name="thumbnails",
    workflows=[ThumbnailBacklogWorkflow],
    build_activities=_activities,
    schedules=thumbnail_schedules(RenderSettings()),
)
