"""The object store (raw staging, its cleanup, the processed-JPEG check), as a
stage: activities only. The parents that stage and clean up are the other
stages', and call them through `object_store.steps`."""

from fishsense_services_api.raw_staging_store import RawStagingCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.object_store.cleanup import RawCleanupActivities
from fishsense_services_orchestrator.object_store.jpegs import ProcessedJpegActivities
from fishsense_services_orchestrator.object_store.staging import (
    RawStagingActivities,
    RawStagingSettings,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, Stage


def _activities(deps: Deps):
    # Read here, at the worker's start: a missing setting fails the start.
    store = OrchestratorObjectStore.from_settings(ObjectStoreConnection())
    catalog = RawStagingCatalog(deps.engine, sub=deps.sub)
    staging = RawStagingActivities(
        catalog=catalog,
        store=store,
        nas_settings=NasSettings(),
        staging_settings=RawStagingSettings(),
    )
    cleanup = RawCleanupActivities(catalog=catalog, store=store)
    jpegs = ProcessedJpegActivities(catalog=catalog, store=store)
    return [
        staging.stage_raw_bytes_for_dive,
        cleanup.cleanup_raw_bytes_for_dive,
        jpegs.locate_processed_jpeg,
    ]


STAGE = Stage(name="object_store", workflows=[], build_activities=_activities)
