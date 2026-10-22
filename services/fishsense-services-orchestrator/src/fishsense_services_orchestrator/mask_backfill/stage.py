"""The mask backfill as an orchestrator stage: workflows and activities
registered, no schedule (it is run by hand, `mask_backfill.run`)."""

from __future__ import annotations

from fishsense_services_api.mask_backfill_store import MaskBackfillCatalog
from fishsense_services_api.renders_store import RendersCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.mask_backfill.activities import (
    MaskBackfillActivities,
)
from fishsense_services_orchestrator.mask_backfill.workflow import (
    MaskBackfillForMissionWorkflow,
    MaskBackfillRunWorkflow,
)
from fishsense_services_orchestrator.object_store.store import OrchestratorObjectStore
from fishsense_services_orchestrator.registry import Deps, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores
from fishsense_services_orchestrator.renders.urls import TenantRenderUrls

__all__ = ["STAGE"]


def _activities(deps: Deps):
    activities = MaskBackfillActivities(
        catalog=MaskBackfillCatalog(deps.engine, sub=deps.sub),
        renders=TenantRenderUrls(
            catalog=RendersCatalog(deps.engine, sub=deps.sub),
            stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
        ),
        store=OrchestratorObjectStore.from_settings(ObjectStoreConnection()),
    )
    return [
        activities.plan_mask_backfill,
        activities.resolve_mask_backfill_page,
        activities.persist_mask_backfill,
    ]


STAGE = Stage(
    name="mask_backfill",
    workflows=[MaskBackfillRunWorkflow, MaskBackfillForMissionWorkflow],
    build_activities=_activities,
    schedules=[],
)
