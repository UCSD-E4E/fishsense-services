"""BioCLIP species pre-annotation (new in v2), as a stage.

**Ships disabled.** The predict schedule exists only when
`FISHSENSE_SPECIES_PREDICTION_ENABLED` is true (`settings`), read when the
worker imports its stages; the workflows and activities are registered either
way. Schedules are created if missing and never updated in place
(`schedules`), so turning the stage off again means deleting
`predict-species-images` as well.

Enabled, it fires hourly at +36: after head/tail predicts at +32, whose kept
masks it crops. Species populate (+20) seeds the suggestions it stored. The
settings resolve when the worker starts, so a stage missing configuration
fails the start, not its first run.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.species_prediction_store import SpeciesPredictionCatalog
from fishsense_services_api.species_store import SpeciesCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioSettings,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage
from fishsense_services_orchestrator.species_predict.activities import (
    SpeciesPredictActivities,
)
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)
from fishsense_services_orchestrator.species_predict.workflow import (
    PREDICT_RUN_TIMEOUT,
    BackfillSpeciesPredictionsWorkflow,
    PredictSpeciesImagesParentWorkflow,
)

__all__ = ["STAGE", "species_prediction_schedules"]


def species_prediction_schedules(
    settings: SpeciesPredictionSettings,
) -> list[ScheduledWorkflow]:
    """The predict parent's schedule, or none while the stage is disabled."""
    if not settings.enabled:
        return []
    return [
        ScheduledWorkflow(
            schedule_id="predict-species-images",
            workflow=PredictSpeciesImagesParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=36),
            run_timeout=PREDICT_RUN_TIMEOUT,
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ]


def _activities(deps: Deps):
    label_studio = LabelStudioSettings()
    activities = SpeciesPredictActivities(
        catalog=SpeciesPredictionCatalog(deps.engine, sub=deps.sub),
        species_catalog=SpeciesCatalog(deps.engine, sub=deps.sub),
        store=OrchestratorObjectStore.from_settings(ObjectStoreConnection()),
        settings=SpeciesPredictionSettings(),
        label_studio_factory=lambda: LabelStudioClient.from_settings(label_studio),
    )
    return [
        activities.select_next_dive_for_species_prediction,
        activities.resolve_species_predict_inputs,
        activities.persist_species_predictions,
        activities.backfill_species_predictions_for_dive,
    ]


STAGE = Stage(
    name="species_predict",
    workflows=[PredictSpeciesImagesParentWorkflow, BackfillSpeciesPredictionsWorkflow],
    build_activities=_activities,
    schedules=species_prediction_schedules(SpeciesPredictionSettings()),
)
