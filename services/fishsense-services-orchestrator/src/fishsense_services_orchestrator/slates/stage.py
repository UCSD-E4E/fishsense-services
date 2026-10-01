"""Stage 9, the dive-slate Label Studio project and its sync, as a stage.

Schedules are v1's (fishsense-lite@77e8f8e5 worker.py): stage 9 hourly at +45
with a 1 h run timeout, skipping on overlap; the slate sync hourly on the hour
with a 3 h run timeout, overlap allowed -- a cursor only moves forward, so two
runs cannot rewind each other.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.label_project_store import LabelProjectCatalog
from fishsense_services_api.slate_store import SlateCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
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
from fishsense_services_orchestrator.slates.activities import SlateActivities
from fishsense_services_orchestrator.slates.pdfs import SlatePdfs
from fishsense_services_orchestrator.slates.populate import (
    DiveSlateProjectActivities,
)
from fishsense_services_orchestrator.slates.sync import SlateSyncActivities
from fishsense_services_orchestrator.slates.workflows import (
    CreateDiveSlateLabelStudioProjectWorkflow,
    PopulateDiveSlateLabelStudioProjectWorkflow,
    PreprocessSlateImagesParentWorkflow,
    SyncLabelStudioDiveSlateLabelsWorkflow,
)


def _activities(deps: Deps):
    # Read here, at the worker's start: a missing setting fails the start.
    store = OrchestratorObjectStore.from_settings(ObjectStoreConnection())
    catalog = SlateCatalog(deps.engine, sub=deps.sub)
    pdfs = SlatePdfs(catalog=catalog, store=store, nas_settings=NasSettings())
    label_studio_settings = LabelStudioSettings()
    label_studio = LabelStudioClient.from_settings(label_studio_settings)
    projects = LabelProjects(
        catalog=LabelProjectCatalog(deps.engine, sub=deps.sub),
        label_studio=label_studio,
        workspace=label_studio_settings.workspace,
        storage=LabelStudioStorageSettings(),
    )
    stage9 = SlateActivities(catalog=catalog, store=store, pdfs=pdfs)
    project = DiveSlateProjectActivities(
        catalog=catalog, label_projects=projects, label_studio=label_studio, store=store
    )
    sync = SlateSyncActivities(
        catalog=catalog,
        label_studio_factory=lambda: LabelStudioClient.from_settings(
            label_studio_settings
        ),
        pdfs=pdfs,
    )
    return [
        stage9.select_next_dive_for_slate_preprocessing,
        stage9.resolve_slate_preprocess_inputs,
        stage9.stage_slate_pdf,
        stage9.clear_slate_reprocess_flags,
        project.create_dive_slate_label_studio_project,
        project.populate_dive_slate_label_studio_project,
        sync.slate_label_projects,
        sync.sync_slate_labels,
    ]


STAGE = Stage(
    name="slates",
    workflows=[
        PreprocessSlateImagesParentWorkflow,
        CreateDiveSlateLabelStudioProjectWorkflow,
        PopulateDiveSlateLabelStudioProjectWorkflow,
        SyncLabelStudioDiveSlateLabelsWorkflow,
    ],
    build_activities=_activities,
    schedules=[
        ScheduledWorkflow(
            schedule_id="preprocess-slate-images",
            workflow=PreprocessSlateImagesParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=45),
            run_timeout=timedelta(hours=1),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        ScheduledWorkflow(
            schedule_id="sync-label-studio-dive-slate-labels",
            workflow=SyncLabelStudioDiveSlateLabelsWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(0),
            run_timeout=timedelta(hours=3),
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        ),
    ],
)
