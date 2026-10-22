"""The shadow calibration as an orchestrator stage (docs/shadow-size-constancy.md).

Its schedule exists only with ``FISHSENSE_SHADOW_CALIBRATION_ENABLED``
(default off); its workflows and activities are registered either way, so a
mission can be run by hand.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.renders_store import RendersCatalog
from fishsense_services_api.shadow_calibration_store import ShadowCalibrationCatalog
from fishsense_services_api.slate_presence_store import slate_frames
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores
from fishsense_services_orchestrator.renders.urls import TenantRenderUrls
from fishsense_services_orchestrator.shadow_calibration.activities import (
    ShadowCalibrationActivities,
)
from fishsense_services_orchestrator.shadow_calibration.settings import (
    ShadowCalibrationSettings,
)
from fishsense_services_orchestrator.shadow_calibration.workflow import (
    DRAIN_WINDOW,
    ShadowCalibrationForMissionWorkflow,
    ShadowCalibrationParentWorkflow,
)

__all__ = ["STAGE", "shadow_calibration_schedules"]


def shadow_calibration_schedules(settings: ShadowCalibrationSettings) -> list:
    """The hourly schedule, or none while the stage is off (the default)."""
    if not settings.enabled:
        return []
    return [
        ScheduledWorkflow(
            schedule_id="shadow-calibration",
            workflow=ShadowCalibrationParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=38),
            run_timeout=DRAIN_WINDOW + timedelta(hours=12),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ]


def _activities(deps: Deps):
    activities = ShadowCalibrationActivities(
        catalog=ShadowCalibrationCatalog(
            deps.engine, sub=deps.sub, slate_frames=slate_frames
        ),
        renders=TenantRenderUrls(
            catalog=RendersCatalog(deps.engine, sub=deps.sub),
            stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
        ),
    )
    return [
        activities.select_next_mission_for_shadow_calibration,
        activities.resolve_shadow_calibration_inputs,
        activities.assemble_shadow_fit,
        activities.persist_shadow_calibration,
    ]


STAGE = Stage(
    name="shadow_calibration",
    workflows=[ShadowCalibrationParentWorkflow, ShadowCalibrationForMissionWorkflow],
    build_activities=_activities,
    schedules=shadow_calibration_schedules(ShadowCalibrationSettings()),
)
