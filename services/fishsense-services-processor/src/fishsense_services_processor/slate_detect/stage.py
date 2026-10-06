"""The slate presence detector (new in v2), as a processor stage.

On the GPU role, beside laser prediction, which it is shaped like: torch is
only in the GPU image, and the role's memory ceiling (2 activities, 6 Gi)
already covers a full-res raw decode per activity. Measured on one 4014x3016
`.ORF` (2026-10-05, a 16-thread laptop, RTX 3060): the production decode takes
15-18 s (the process, torch loaded, peaked at 5.6 GB RSS); rectifying 0.1 s;
the 1600 px JPEG 0.2 s; the model with its flip 0.02 s on the GPU, and 0.70 /
0.44 / 0.33 s on 1 / 2 / 4 CPU threads. So the decode is ~95% of a frame
either way: the queue's CPU fallback is barely slower, and the light role
could not run it at all -- no torch in its image, and a 2 Gi pod against a
decode the per-image role sizes at 1-3 GB.

The object store, the weight store and the pin are read on first use, not
here: the registry imports every stage wherever it runs, and only a pod
serving this queue ever fetches the weights.
"""

from functools import cache

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.slate_detect.activities import (
    SlateDetectActivities,
)
from fishsense_services_processor.slate_detect.weights import (
    SlateDetectorSettings,
    fetch_slate_detector,
)
from fishsense_services_processor.slate_detect.workflow import (
    DetectSlatePresenceWorkflow,
)
from fishsense_services_processor.weights import (
    GarageWeightStore,
    ModelWeightsSettings,
)


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


@cache
def _weights() -> tuple[GarageWeightStore, ModelWeightsSettings, SlateDetectorSettings]:
    settings = ModelWeightsSettings()
    return GarageWeightStore.from_settings(settings), settings, SlateDetectorSettings()


async def _slate_detector_weights():
    store, settings, pin = _weights()
    return await fetch_slate_detector(
        store=store, cache_dir=settings.cache_dir, settings=pin
    )


_ACTIVITIES = SlateDetectActivities(
    store_factory=_store, weights=_slate_detector_weights
)

STAGE = Stage(
    name="slate_detect",
    role=ROLE_GPU,
    workflows=[DetectSlatePresenceWorkflow],
    activities=[_ACTIVITIES.detect_slate_presence],
)
