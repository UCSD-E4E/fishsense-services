"""The laser-color backfill as an orchestrator stage: no schedule (run by
hand, `laser_colour_backfill.run`). Writes only with ``--apply``."""

from __future__ import annotations

from fishsense_services_api.laser_colour_backfill_store import ColourBackfillCatalog
from fishsense_services_api.renders_store import RendersCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.laser_colour_backfill.activities import (
    ColourBackfillActivities,
)
from fishsense_services_orchestrator.laser_colour_backfill.workflow import (
    BackfillLaserColoursForMissionWorkflow,
    BackfillLaserColoursWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores
from fishsense_services_orchestrator.renders.urls import TenantRenderUrls

__all__ = ["STAGE"]


def _activities(deps: Deps):
    activities = ColourBackfillActivities(
        catalog=ColourBackfillCatalog(deps.engine, sub=deps.sub),
        renders=TenantRenderUrls(
            catalog=RendersCatalog(deps.engine, sub=deps.sub),
            stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
        ),
    )
    return [
        activities.plan_colour_backfill,
        activities.resolve_colour_page,
        activities.record_colour_page,
    ]


STAGE = Stage(
    name="laser_colour_backfill",
    workflows=[BackfillLaserColoursWorkflow, BackfillLaserColoursForMissionWorkflow],
    build_activities=_activities,
    schedules=[],
)
