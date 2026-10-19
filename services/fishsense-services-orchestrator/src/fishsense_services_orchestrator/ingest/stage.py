"""Dive ingest, as a stage."""

from fishsense_services_api.ingest_store import IngestCatalog
from fishsense_services_orchestrator.ingest.activities import IngestActivities
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ingest.workflow import IngestMissionWorkflow
from fishsense_services_orchestrator.registry import Deps, Stage


def _activities(deps: Deps):
    ingest = IngestActivities(
        nas_settings=NasSettings(), catalog=IngestCatalog(deps.engine, sub=deps.sub)
    )
    return [
        ingest.list_mission_folder,
        ingest.preflight,
        ingest.create_mission,
        ingest.scan_and_register,
        ingest.finalize_mission,
    ]


STAGE = Stage(
    name="ingest", workflows=[IngestMissionWorkflow], build_activities=_activities
)
