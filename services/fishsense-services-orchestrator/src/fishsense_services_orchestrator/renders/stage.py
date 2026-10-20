"""The render stage (new in v2, 2026-10-19).

Its activities hold the tenant stores, as the API does
(`FISHSENSE_TENANT_STORES_FILE`), to presign each render's PUT and check what
was stored; with none configured, no tenant has a bucket and nothing is
rendered. The hourly backlog schedule exists only when
`FISHSENSE_RENDERS_ENABLED` is true; the workflows are registered either way.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.renders_store import RendersCatalog
from fishsense_services_api.tenant_stores import TenantStores
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage
from fishsense_services_orchestrator.renders.activities import RenderActivities
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.workflow import (
    RenderBacklogWorkflow,
    RenderMissionWorkflow,
)

__all__ = ["STAGE", "render_schedules", "tenant_stores"]


def tenant_stores(
    settings: RenderSettings, connection: ObjectStoreConnection
) -> TenantStores:
    """The tenant stores the API reads; none configured is no bucket at all."""
    if settings.tenant_stores_file is None:
        return TenantStores(
            endpoint_url=connection.endpoint_url,
            public_endpoint_url=connection.endpoint_url,
            region=connection.region,
            stores={},
        )
    return TenantStores.from_file(
        settings.tenant_stores_file,
        endpoint_url=connection.endpoint_url,
        public_endpoint_url=settings.object_store_public_url or connection.endpoint_url,
        region=connection.region,
    )


def render_schedules(settings: RenderSettings) -> list[ScheduledWorkflow]:
    """The backlog's schedule, or none while renders run on demand only."""
    if not settings.renders_enabled:
        return []
    return [
        ScheduledWorkflow(
            schedule_id="render-backlog",
            workflow=RenderBacklogWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=12),
            run_timeout=timedelta(hours=23),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ]


def _activities(deps: Deps):
    connection = ObjectStoreConnection()
    activities = RenderActivities(
        catalog=RendersCatalog(deps.engine, sub=deps.sub),
        store=OrchestratorObjectStore.from_settings(connection),
        stores=tenant_stores(RenderSettings(), connection),
    )
    return [
        activities.select_render_missions,
        activities.resolve_render_inputs,
        activities.persist_renders,
    ]


STAGE = Stage(
    name="renders",
    workflows=[RenderMissionWorkflow, RenderBacklogWorkflow],
    build_activities=_activities,
    schedules=render_schedules(RenderSettings()),
)
