"""The laser slice, as a stage.

The schedules are v1's (fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
worker.py): hourly, each at v1's minute -- preprocess at +0, predict at +10,
populate at +12 (after predict: populate is prediction-gated), the gate's
drain at +22 -- skipping on overlap, with v1's run timeouts.
"""

from datetime import timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.label_project_store import LabelProjectCatalog
from fishsense_services_api.laser_store import LaserCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioSettings,
)
from fishsense_services_orchestrator.labels.populate import (
    LabelProjects,
    LabelStudioStorageSettings,
)
from fishsense_services_orchestrator.laser.activities import LaserActivities
from fishsense_services_orchestrator.laser.workflow import (
    BackfillLaserPredictionsWorkflow,
    CreateLaserLabelStudioProjectWorkflow,
    EvaluateLaserAutoAcceptParentWorkflow,
    PopulateLaserLabelStudioProjectParentWorkflow,
    PopulateLaserLabelStudioProjectWorkflow,
    PredictLaserImagesParentWorkflow,
    PreprocessLaserImagesParentWorkflow,
    RemediateDiveLaserSupersedesWorkflow,
    RemediateLaserSupersedesParentWorkflow,
    ValidateDiveLaserLabelsWorkflow,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


class LaserLabelStudioSettings(BaseSettings):
    """The Label Studio service account auto-accepted annotations are
    attributed to (``FISHSENSE_LABEL_STUDIO_BOT_USER_ID``; v1's
    `label_studio.bot_user_id`). 0: unconfigured, and omitted."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_LABEL_STUDIO_")

    bot_user_id: int = 0


def _activities(deps: Deps):
    # Read here, at the worker's start: a missing setting fails the start.
    label_studio_settings = LabelStudioSettings()
    label_studio = LabelStudioClient.from_settings(label_studio_settings)
    laser = LaserActivities(
        catalog=LaserCatalog(deps.engine, sub=deps.sub),
        store=OrchestratorObjectStore.from_settings(ObjectStoreConnection()),
        label_studio=label_studio,
        label_projects=LabelProjects(
            catalog=LabelProjectCatalog(deps.engine, sub=deps.sub),
            label_studio=label_studio,
            workspace=label_studio_settings.workspace,
            storage=LabelStudioStorageSettings(),
        ),
        bot_user_id=LaserLabelStudioSettings().bot_user_id,
    )
    return [
        laser.select_next_dive_for_laser_preprocessing,
        laser.resolve_laser_preprocess_inputs,
        laser.clear_laser_reprocess_flags,
        laser.select_next_dive_for_laser_prediction,
        laser.resolve_laser_predict_inputs,
        laser.persist_laser_predictions,
        laser.backfill_laser_predictions_for_dive,
        laser.select_next_dive_for_laser_auto_accept,
        laser.resolve_laser_gate_inputs,
        laser.record_laser_gate_verdicts,
        laser.apply_laser_auto_accept_for_dive,
        laser.select_dives_needing_laser_population,
        laser.create_laser_label_studio_project,
        laser.populate_laser_label_studio_project,
        laser.laser_dives_with_complete_labeling,
        laser.resolve_laser_validation_inputs,
        laser.apply_laser_validation,
        laser.resolve_laser_remediation_dives,
        laser.resolve_laser_remediation_inputs,
        laser.apply_laser_remediation,
    ]


def _hourly(schedule_id, workflow, *, minute, run_timeout):
    return ScheduledWorkflow(
        schedule_id=schedule_id,
        workflow=workflow,
        every=timedelta(hours=1),
        offset=timedelta(minutes=minute),
        run_timeout=run_timeout,
        overlap=ScheduleOverlapPolicy.SKIP,
    )


STAGE = Stage(
    name="laser",
    workflows=[
        PreprocessLaserImagesParentWorkflow,
        PredictLaserImagesParentWorkflow,
        EvaluateLaserAutoAcceptParentWorkflow,
        PopulateLaserLabelStudioProjectParentWorkflow,
        PopulateLaserLabelStudioProjectWorkflow,
        CreateLaserLabelStudioProjectWorkflow,
        BackfillLaserPredictionsWorkflow,
        ValidateDiveLaserLabelsWorkflow,
        RemediateLaserSupersedesParentWorkflow,
        RemediateDiveLaserSupersedesWorkflow,
    ],
    build_activities=_activities,
    schedules=[
        _hourly(
            "preprocess-laser-images",
            PreprocessLaserImagesParentWorkflow,
            minute=0,
            run_timeout=timedelta(hours=1),
        ),
        _hourly(
            "predict-laser-images",
            PredictLaserImagesParentWorkflow,
            minute=10,
            run_timeout=timedelta(hours=2),
        ),
        _hourly(
            "populate-laser-labels",
            PopulateLaserLabelStudioProjectParentWorkflow,
            minute=12,
            run_timeout=timedelta(hours=1),
        ),
        _hourly(
            "evaluate-laser-auto-accept",
            EvaluateLaserAutoAcceptParentWorkflow,
            minute=22,
            run_timeout=timedelta(hours=1),
        ),
    ],
)
