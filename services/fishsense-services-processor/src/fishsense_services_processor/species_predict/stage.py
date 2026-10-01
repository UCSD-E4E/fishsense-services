"""BioCLIP species pre-annotation (new in v2), as a processor stage.

On the GPU role, beside head/tail prediction: BioCLIP 2.5 is a ViT-H/14, and
the queue prefers a GPU. The CPU-fallback Deployment serves the same queue,
where the model runs in fp32 on the CPU (slower, same answer).

The object store, the weight store and the BioCLIP pins are read on first
use, not here: the registry imports every stage wherever it runs, and only a
pod serving this queue ever fetches BioCLIP.
"""

from functools import cache

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.species_predict.activities import (
    SpeciesPredictActivities,
)
from fishsense_services_processor.species_predict.weights import (
    BioclipSettings,
    fetch_bioclip,
)
from fishsense_services_processor.species_predict.workflow import (
    PredictSpeciesImagesWorkflow,
)
from fishsense_services_processor.weights import (
    GarageWeightStore,
    ModelWeightsSettings,
)


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


@cache
def _weights() -> tuple[GarageWeightStore, ModelWeightsSettings, BioclipSettings]:
    settings = ModelWeightsSettings()
    return GarageWeightStore.from_settings(settings), settings, BioclipSettings()


async def _bioclip_weights(model_id: str):
    store, settings, bioclip = _weights()
    return await fetch_bioclip(
        model_id, store=store, cache_dir=settings.cache_dir, settings=bioclip
    )


_ACTIVITIES = SpeciesPredictActivities(
    store_factory=_store, bioclip_weights=_bioclip_weights
)

STAGE = Stage(
    name="species_predict",
    role=ROLE_GPU,
    workflows=[PredictSpeciesImagesWorkflow],
    activities=[_ACTIVITIES.predict_species_image],
)
