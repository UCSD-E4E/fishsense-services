"""Laser depth, as a stage."""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.laser_depth_store import LaserDepthCatalog
from fishsense_services_orchestrator.laser_depth.activities import (
    LaserDepthActivities,
)
from fishsense_services_orchestrator.laser_depth.workflow import (
    ComputeLaserDepthsParentWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(deps: Deps):
    depths = LaserDepthActivities(catalog=LaserDepthCatalog(deps.engine, sub=deps.sub))
    return [
        depths.select_next_dive_for_laser_depth,
        depths.resolve_laser_depth_inputs,
        depths.persist_laser_depths,
    ]


STAGE = Stage(
    name="laser_depth",
    workflows=[ComputeLaserDepthsParentWorkflow],
    build_activities=_activities,
    schedules=[
        # v1's compute-laser-depths-workflow-schedule (fishsense-lite@77e8f8e5
        # worker.py): hourly at :35, run timeout 1 h 30, skipping on overlap.
        # Before calibration (:50), clear of the :55 sweeper.
        ScheduledWorkflow(
            schedule_id="compute-laser-depths",
            workflow=ComputeLaserDepthsParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=35),
            run_timeout=timedelta(hours=1, minutes=30),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ],
)
