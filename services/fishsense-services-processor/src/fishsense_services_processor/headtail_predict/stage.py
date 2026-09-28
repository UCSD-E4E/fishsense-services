"""Head/tail prediction (SAM 3.1), as a processor stage.

On the GPU role: its queue prefers a GPU and is served by the CPU-fallback
Deployment when none starts, where the stage runs fishsense-core's Mask R-CNN
instead (v1's GPU data-worker, fishsense-lite@77e8f8e5 roles.py).

The object store, the weight store and the SAM 3.1 pin are read on first use,
not here: the registry imports every stage wherever it runs, and only a pod
with a GPU ever fetches SAM 3.1's weights.
"""

from functools import cache

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.headtail_predict.activities import (
    HeadtailPredictActivities,
)
from fishsense_services_processor.headtail_predict.weights import (
    Sam3Settings,
    fetch_sam3,
)
from fishsense_services_processor.headtail_predict.workflow import (
    PredictHeadtailImagesWorkflow,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.weights import (
    GarageWeightStore,
    ModelWeightsSettings,
)


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


@cache
def _weights() -> tuple[GarageWeightStore, ModelWeightsSettings, Sam3Settings]:
    settings = ModelWeightsSettings()
    return GarageWeightStore.from_settings(settings), settings, Sam3Settings()


async def _sam3_checkpoint():
    store, settings, sam3 = _weights()
    return await fetch_sam3(store=store, cache_dir=settings.cache_dir, settings=sam3)


_ACTIVITIES = HeadtailPredictActivities(
    store_factory=_store, sam3_checkpoint=_sam3_checkpoint
)

STAGE = Stage(
    name="headtail_predict",
    role=ROLE_GPU,
    workflows=[PredictHeadtailImagesWorkflow],
    activities=[_ACTIVITIES.predict_headtail_image],
)
