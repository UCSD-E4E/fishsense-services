"""The phone measurement as an orchestrator stage: workflows and activities
registered, no schedule (it is run by hand, `phone_measurement.run`)."""

from __future__ import annotations

from fishsense_services_api.phone_measurement_store import PhoneMeasurementCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.phone_measurement.activities import (
    PhoneMeasurementActivities,
)
from fishsense_services_orchestrator.phone_measurement.workflow import (
    PhoneMeasurementForMissionWorkflow,
    PhoneMeasurementRunWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores

__all__ = ["STAGE"]


def _activities(deps: Deps):
    activities = PhoneMeasurementActivities(
        catalog=PhoneMeasurementCatalog(deps.engine, sub=deps.sub),
        stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
    )
    return [
        activities.plan_phone_measurement,
        activities.resolve_phone_measurement_page,
        activities.persist_phone_measurements,
    ]


STAGE = Stage(
    name="phone_measurement",
    workflows=[PhoneMeasurementRunWorkflow, PhoneMeasurementForMissionWorkflow],
    build_activities=_activities,
    schedules=[],
)
