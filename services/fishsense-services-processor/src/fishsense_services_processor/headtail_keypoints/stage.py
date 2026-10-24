"""The SimCC keypoint replay as a processor stage on the GPU role. The model
is fishsense-core's pinned checkpoint (the same in 4.2.0 and 4.3.0),
fetched from Garage's model-weights bucket through core's verified fetch (the processor-secret sync
uploads it there from its pinned Hugging Face revision), loaded once per
worker. Read-only: run by the replay alone."""

from __future__ import annotations

import threading
from typing import Any

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.headtail_keypoints.activities import (
    HeadtailKeypointActivities,
)
from fishsense_services_processor.headtail_keypoints.workflow import (
    PredictHeadtailKeypointsWorkflow,
)
from fishsense_services_processor.headtail_predict.predict import cuda_available
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, Stage

__all__ = ["CHECKPOINT", "STAGE"]

#: core's manifest entry (name/version) for the model: pinned since 4.2.0,
#: unchanged in 4.3.0 (simcc-sam-e12 8918034a0f84).
CHECKPOINT = "headtail-keypoints/simcc-sam-e12"

_DETECTOR: Any = None
_LOCK = threading.Lock()


def _detector() -> Any:
    """The verified model, loaded on first use (double-checked lock)."""
    global _DETECTOR  # pylint: disable=global-statement
    if _DETECTOR is None:
        with _LOCK:
            if _DETECTOR is None:
                # pylint: disable-next=import-outside-toplevel
                from fishsense_core.fish.headtail_keypoints import (
                    HeadTailKeypointDetector,
                )

                # pylint: disable-next=import-outside-toplevel
                from fishsense_services_processor.weights import (
                    GarageWeightStore,
                    ModelWeightsSettings,
                )

                settings = ModelWeightsSettings()
                _DETECTOR = HeadTailKeypointDetector.from_store(
                    GarageWeightStore.from_settings(settings),
                    cache_dir=settings.cache_dir,
                    version=CHECKPOINT.split("/")[1],
                )
    return _DETECTOR


_ACTIVITIES = HeadtailKeypointActivities(
    store_factory=lambda: ProcessorObjectStore.from_settings(ObjectStoreConnection()),
    detector=_detector,
    checkpoint=CHECKPOINT,
    cuda_available=cuda_available,
)

STAGE = Stage(
    name="headtail_keypoints",
    role=ROLE_GPU,
    workflows=[PredictHeadtailKeypointsWorkflow],
    activities=[_ACTIVITIES.predict_headtail_keypoints],
)
