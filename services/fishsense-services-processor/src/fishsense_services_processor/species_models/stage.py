"""The model-class replay as a processor stage on the GPU role, over the
species stage's BioCLIP (loaded once per worker, its verified weights and its
out-of-memory fall-back). Read-only: run by the replay alone."""

from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.species_models.activities import (
    SpeciesModelsActivities,
)
from fishsense_services_processor.species_models.workflow import (
    ScoreSpeciesModelsWorkflow,
)
from fishsense_services_processor.species_predict import stage as species_stage

_ACTIVITIES = SpeciesModelsActivities(
    store_factory=species_stage._store,  # pylint: disable=protected-access
    classifier=species_stage._ACTIVITIES._loaded,  # pylint: disable=protected-access
)

STAGE = Stage(
    name="species_models",
    role=ROLE_GPU,
    workflows=[ScoreSpeciesModelsWorkflow],
    activities=[_ACTIVITIES.score_species_models],
)
