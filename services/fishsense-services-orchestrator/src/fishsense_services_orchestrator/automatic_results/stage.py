"""Automatic results (new in v2), as a stage.

**Ships disabled.** The backlog schedule exists only when
`FISHSENSE_AUTOMATIC_RESULTS_ENABLED` is true (`settings`); the workflows and
activities are registered either way, so a named dive can be run by hand.
Enabled, it fires hourly at +48 -- after head/tail (+32) and species (+36)
predict, whose GPU it shares -- and skips on overlap: one dive per run.

The slate-presence detector is being built in parallel; until it lands the
catalog's `slate_frames` is the store's stub (no slate frames), so every
backlog dive's lengths fall back to its effective stored calibration where it
has one. Wire its store function here when it lands.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.automatic_results_store import AutomaticResultsCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.automatic_results.activities import (
    AutomaticResultsActivities,
)
from fishsense_services_orchestrator.automatic_results.settings import (
    AutomaticResultsSettings,
)
from fishsense_services_orchestrator.automatic_results.workflow import (
    RUN_TIMEOUT,
    AutomaticResultsForDiveWorkflow,
    AutomaticResultsParentWorkflow,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage

__all__ = ["STAGE", "automatic_results_schedules"]


def automatic_results_schedules(
    settings: AutomaticResultsSettings,
) -> list[ScheduledWorkflow]:
    """The backlog's schedule, or none while the track is disabled."""
    if not settings.enabled:
        return []
    return [
        ScheduledWorkflow(
            schedule_id="automatic-results",
            workflow=AutomaticResultsParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=48),
            run_timeout=RUN_TIMEOUT,
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ]


def _activities(deps: Deps):
    activities = AutomaticResultsActivities(
        catalog=AutomaticResultsCatalog(deps.engine, sub=deps.sub),
        store=OrchestratorObjectStore.from_settings(ObjectStoreConnection()),
    )
    return [
        activities.select_next_dive_for_automatic_results,
        activities.resolve_automatic_frames_inputs,
        activities.persist_automatic_frames,
        activities.resolve_automatic_species_inputs,
        activities.persist_automatic_species,
        activities.resolve_automatic_calibration_inputs,
        activities.persist_automatic_calibration,
        activities.resolve_automatic_measure_inputs,
        activities.persist_automatic_measurements,
    ]


STAGE = Stage(
    name="automatic_results",
    workflows=[AutomaticResultsParentWorkflow, AutomaticResultsForDiveWorkflow],
    build_activities=_activities,
    schedules=automatic_results_schedules(AutomaticResultsSettings()),
)
