"""The phone species as an orchestrator stage: workflows and activities
registered, no schedule (it is run by hand, `phone_species.run`)."""

from __future__ import annotations

from fishsense_services_api.phone_species_store import PhoneSpeciesCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.phone_species.activities import (
    PhoneSpeciesActivities,
)
from fishsense_services_orchestrator.phone_species.workflow import (
    PhoneSpeciesForMissionWorkflow,
    PhoneSpeciesRunWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores

__all__ = ["STAGE"]


def _activities(deps: Deps):
    activities = PhoneSpeciesActivities(
        catalog=PhoneSpeciesCatalog(deps.engine, sub=deps.sub),
        stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
    )
    return [
        activities.plan_phone_species,
        activities.resolve_phone_species_page,
        activities.persist_phone_species,
    ]


STAGE = Stage(
    name="phone_species",
    workflows=[PhoneSpeciesRunWorkflow, PhoneSpeciesForMissionWorkflow],
    build_activities=_activities,
    schedules=[],
)
