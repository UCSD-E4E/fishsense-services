"""Head/tail as a stage: stage 5.1, SAM 3.1 prediction, Label Studio, the sync.

The settings resolve when the worker starts (Label Studio, its S3 storage, the
object store), so a stage missing configuration fails the start, not its
first run. v1's schedules, minutes and run timeouts (fishsense-lite@77e8f8e5
fishsense_api_workflow_worker/worker.py); the ids are v2's own, since
Temporal is shared until cutover, and predict's run timeout covers its child
(v1's did not).
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.headtail_store import HeadtailCatalog
from fishsense_services_api.label_project_store import LabelProjectCatalog
from fishsense_services_api.label_sync_store import LabelSyncCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.headtail.activities import HeadtailActivities
from fishsense_services_orchestrator.headtail.populate import (
    HeadtailLabelActivities,
)
from fishsense_services_orchestrator.headtail.sync import HeadTailSyncActivities
from fishsense_services_orchestrator.headtail.workflow import (
    PREDICT_RUN_TIMEOUT,
    BackfillHeadtailPredictionsWorkflow,
    CreateHeadTailLabelStudioProjectWorkflow,
    PopulateHeadTailLabelStudioProjectParentWorkflow,
    PopulateHeadTailLabelStudioProjectWorkflow,
    PredictHeadtailImagesParentWorkflow,
    PreprocessHeadtailImagesParentWorkflow,
    SyncLabelStudioHeadTailLabelsWorkflow,
)
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


def _activities(deps: Deps):
    label_studio = LabelStudioSettings()
    storage = LabelStudioStorageSettings()
    store = OrchestratorObjectStore.from_settings(ObjectStoreConnection())
    catalog = HeadtailCatalog(deps.engine, sub=deps.sub)

    def _label_studio() -> LabelStudioClient:
        return LabelStudioClient.from_settings(label_studio)

    def _label_projects() -> LabelProjects:
        return LabelProjects(
            catalog=LabelProjectCatalog(deps.engine, sub=deps.sub),
            label_studio=_label_studio(),
            workspace=label_studio.workspace,
            storage=storage,
        )

    headtail = HeadtailActivities(catalog=catalog, store=store)
    labels = HeadtailLabelActivities(
        catalog=catalog,
        store=store,
        label_studio_factory=_label_studio,
        label_projects_factory=_label_projects,
    )
    sync = HeadTailSyncActivities(
        catalog=LabelSyncCatalog(deps.engine, sub=deps.sub),
        label_studio_factory=_label_studio,
    )
    return [
        headtail.select_next_dive_for_headtail_preprocessing,
        headtail.resolve_headtail_preprocess_inputs,
        headtail.clear_headtail_reprocess_flags,
        headtail.select_next_dive_for_headtail_prediction,
        headtail.resolve_headtail_predict_inputs,
        headtail.persist_headtail_predictions,
        labels.select_dives_needing_headtail_population,
        labels.create_headtail_label_studio_project,
        labels.populate_headtail_label_studio_project,
        labels.backfill_headtail_predictions_for_dive,
        sync.head_tail_label_projects,
        sync.sync_head_tail_labels,
    ]


_HOURLY = timedelta(hours=1)

STAGE = Stage(
    name="headtail",
    workflows=[
        PreprocessHeadtailImagesParentWorkflow,
        PredictHeadtailImagesParentWorkflow,
        PopulateHeadTailLabelStudioProjectParentWorkflow,
        PopulateHeadTailLabelStudioProjectWorkflow,
        CreateHeadTailLabelStudioProjectWorkflow,
        BackfillHeadtailPredictionsWorkflow,
        SyncLabelStudioHeadTailLabelsWorkflow,
    ],
    build_activities=_activities,
    schedules=[
        # +30: render stage 5.1's JPEGs. A selector skips on overlap.
        ScheduledWorkflow(
            schedule_id="preprocess-headtail-images",
            workflow=PreprocessHeadtailImagesParentWorkflow,
            every=_HOURLY,
            offset=timedelta(minutes=30),
            run_timeout=timedelta(hours=1),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        # +32: predict on what +30 rendered; drains one dive per firing. v2:
        # the run outlives its wake and its 6 h child (v1's 2 h killed the
        # child on the CPU fallback); SKIP holds the next firings meanwhile.
        ScheduledWorkflow(
            schedule_id="predict-headtail-images",
            workflow=PredictHeadtailImagesParentWorkflow,
            every=_HOURLY,
            offset=timedelta(minutes=32),
            run_timeout=PREDICT_RUN_TIMEOUT,
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        # +34: populate, prediction-gated, after +32 has written its rows.
        ScheduledWorkflow(
            schedule_id="populate-headtail-labels",
            workflow=PopulateHeadTailLabelStudioProjectParentWorkflow,
            every=_HOURLY,
            offset=timedelta(minutes=34),
            run_timeout=timedelta(hours=1),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        # On the hour, sized for a first run over a backlog project; overlap
        # is allowed (v1's): a cursor only moves forward.
        ScheduledWorkflow(
            schedule_id="sync-label-studio-head-tail-labels",
            workflow=SyncLabelStudioHeadTailLabelsWorkflow,
            every=_HOURLY,
            offset=timedelta(0),
            run_timeout=timedelta(hours=3),
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        ),
    ],
)
