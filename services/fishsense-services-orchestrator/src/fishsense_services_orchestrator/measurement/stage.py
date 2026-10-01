"""Stage 14, measure fish, as a stage."""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.measurement_store import MeasurementCatalog
from fishsense_services_orchestrator.measurement.activities import (
    MeasurementActivities,
)
from fishsense_services_orchestrator.measurement.workflow import (
    MeasureFishParentWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(deps: Deps):
    measure = MeasurementActivities(
        catalog=MeasurementCatalog(deps.engine, sub=deps.sub)
    )
    return [
        measure.select_next_dive_for_measurement,
        measure.resolve_measurement_inputs,
        measure.persist_measurements,
    ]


STAGE = Stage(
    name="measurement",
    workflows=[MeasureFishParentWorkflow],
    build_activities=_activities,
    schedules=[
        # v1's measure-fish-workflow-schedule (fishsense-lite@77e8f8e5
        # worker.py): hourly at :40, run timeout 1 h 30, skipping on overlap.
        ScheduledWorkflow(
            schedule_id="measure-fish",
            workflow=MeasureFishParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=40),
            run_timeout=timedelta(hours=1, minutes=30),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ],
)
