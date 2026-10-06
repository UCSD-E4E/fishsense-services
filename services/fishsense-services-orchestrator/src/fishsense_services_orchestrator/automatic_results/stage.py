"""Automatic results (new in v2), as a stage.

**Ships disabled.** The backlog schedule exists only when
`FISHSENSE_AUTOMATIC_RESULTS_ENABLED` is true (`settings`); the workflows and
activities are registered either way, so a named dive can be run by hand.
Enabled, it fires hourly at +48 -- after head/tail (+32) and species (+36)
predict, whose GPU it shares -- and skips on overlap: one dive per run.

The catalog reads the slate-presence detector's frames (`slate_presence_store.
slate_frames`, 0035): a frame it calls a slate is never measured as a fish and
is a calibration candidate. A dive the detector hasn't scored yet reads as
having none, so run the detector ahead of this stage.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.automatic_results_store import AutomaticResultsCatalog
from fishsense_services_api.slate_presence_store import slate_frames
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
        # The slate-presence detector's frames: never measured as fish, and
        # calibration candidates (0035).
        catalog=AutomaticResultsCatalog(
            deps.engine, sub=deps.sub, slate_frames=slate_frames
        ),
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
