"""The SimCC keypoint replay as an orchestrator stage: no schedule (run by
hand, `headtail_keypoints.run`). Read-only."""

from __future__ import annotations

from fishsense_services_api.headtail_keypoints_store import HeadtailKeypointCatalog
from fishsense_services_api.renders_store import RendersCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.headtail_keypoints.activities import (
    KeypointReplayActivities,
)
from fishsense_services_orchestrator.headtail_keypoints.workflow import (
    ReplayHeadtailKeypointsForMissionWorkflow,
    ReplayHeadtailKeypointsWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, Stage
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import tenant_stores
from fishsense_services_orchestrator.renders.urls import TenantRenderUrls

__all__ = ["STAGE"]


def _activities(deps: Deps):
    activities = KeypointReplayActivities(
        catalog=HeadtailKeypointCatalog(deps.engine, sub=deps.sub),
        renders=TenantRenderUrls(
            catalog=RendersCatalog(deps.engine, sub=deps.sub),
            stores=tenant_stores(RenderSettings(), ObjectStoreConnection()),
        ),
    )
    return [activities.plan_keypoint_replay, activities.resolve_keypoint_page]


STAGE = Stage(
    name="headtail_keypoints",
    workflows=[
        ReplayHeadtailKeypointsWorkflow,
        ReplayHeadtailKeypointsForMissionWorkflow,
    ],
    build_activities=_activities,
    schedules=[],
)
