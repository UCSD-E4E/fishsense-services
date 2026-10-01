"""The species stages (2, the Label Studio project, 4.2 and 6.1), as a stage.

Schedules are v1's (fishsense-lite@77e8f8e5 fishsense_api_workflow_worker/
worker.py `schedule_workflows`): minute, interval, overlap and run timeout.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.label_project_store import LabelProjectCatalog
from fishsense_services_api.label_sync_store import LabelSyncCatalog
from fishsense_services_api.species_prediction_store import SpeciesPredictionCatalog
from fishsense_services_api.species_store import SpeciesCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioSettings,
)
from fishsense_services_orchestrator.labels.populate import (
    LabelProjects,
    LabelStudioStorageSettings,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage
from fishsense_services_orchestrator.species.activities import SpeciesActivities
from fishsense_services_orchestrator.species.workflows import (
    CreateSpeciesLabelStudioProjectWorkflow,
    PopulateSpeciesLabelStudioProjectParentWorkflow,
    PopulateSpeciesLabelStudioProjectWorkflow,
    PreprocessSpeciesImagesParentWorkflow,
    SyncLabelStudioSpeciesLabelsWorkflow,
    UpdateDiveImageGroupsWorkflow,
)
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)


def _activities(deps: Deps):
    # Read here, at the worker's start: a missing setting fails the start.
    settings = LabelStudioSettings()
    storage = LabelStudioStorageSettings()
    species = SpeciesActivities(
        catalog=SpeciesCatalog(deps.engine, sub=deps.sub),
        store=OrchestratorObjectStore.from_settings(ObjectStoreConnection()),
        sync_catalog=LabelSyncCatalog(deps.engine, sub=deps.sub),
        label_projects=LabelProjects(
            catalog=LabelProjectCatalog(deps.engine, sub=deps.sub),
            label_studio=LabelStudioClient.from_settings(settings),
            workspace=settings.workspace,
            storage=storage,
        ),
        label_studio_factory=lambda: LabelStudioClient.from_settings(settings),
        # v2: BioCLIP's suggestions, seeded only while the pre-annotation
        # stage is enabled (off by default; see species_predict.settings).
        predictions=SpeciesPredictionCatalog(deps.engine, sub=deps.sub),
        prediction_settings=SpeciesPredictionSettings(),
    )
    return [
        species.select_next_dive_for_species_preprocessing,
        species.resolve_species_preprocess_inputs,
        species.clear_species_reprocess_flags,
        species.create_species_label_studio_project,
        species.select_dives_needing_species_population,
        species.populate_species_label_studio_project,
        species.species_label_projects,
        species.sync_species_labels,
        species.update_dive_image_groups,
    ]


STAGE = Stage(
    name="species",
    workflows=[
        PreprocessSpeciesImagesParentWorkflow,
        CreateSpeciesLabelStudioProjectWorkflow,
        PopulateSpeciesLabelStudioProjectWorkflow,
        PopulateSpeciesLabelStudioProjectParentWorkflow,
        SyncLabelStudioSpeciesLabelsWorkflow,
        UpdateDiveImageGroupsWorkflow,
    ],
    build_activities=_activities,
    schedules=[
        # v1's +15: after clustering (+5) and the laser detector (+10). SKIP,
        # so two selectors never pick the same dive; 2 h, the child's own.
        ScheduledWorkflow(
            schedule_id="preprocess-species-images",
            workflow=PreprocessSpeciesImagesParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=15),
            run_timeout=timedelta(hours=2),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        # v1's +20: just after +15 wrote the JPEGs populate gates on.
        ScheduledWorkflow(
            schedule_id="populate-species-labels",
            workflow=PopulateSpeciesLabelStudioProjectParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=20),
            run_timeout=timedelta(hours=1),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        # v1's: hourly on the hour, overlap allowed (a cursor only moves
        # forward), 3 h for a first run over a backlog project.
        ScheduledWorkflow(
            schedule_id="sync-label-studio-species-labels",
            workflow=SyncLabelStudioSpeciesLabelsWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(0),
            run_timeout=timedelta(hours=3),
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        ),
    ],
)
