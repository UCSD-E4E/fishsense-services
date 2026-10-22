"""The shadow calibration as processor stages: SAM on the GPU role (beside the
head/tail and automatic stages, whose verified SAM 3.1 checkpoint and loaded
model it shares), the fit on the light role. Nothing loads at import.

SAM runs at the port's 0.3 threshold on a `Sam3Processor` of its own over the
shared model (`_segmenter`): the shared processor, and so the head/tail
stages, keep their 0.5.
"""

from __future__ import annotations

import threading
from typing import Any

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_contracts.shadow_calibration import SHADOW_SAM_THRESHOLD
from fishsense_services_processor.automatic_results.frames import ScoredSam3Adapter
from fishsense_services_processor.headtail_predict import stage as headtail_stage
from fishsense_services_processor.headtail_predict.activities import (
    HeadtailPredictActivities,
)
from fishsense_services_processor.headtail_predict.predict import (
    cuda_available,
    get_segmenter,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, ROLE_LIGHT, Stage
from fishsense_services_processor.shadow_calibration.activities import (
    ShadowCalibrationActivities,
)
from fishsense_services_processor.shadow_calibration.fit_workflow import (
    FitShadowCalibrationWorkflow,
)
from fishsense_services_processor.shadow_calibration.workflow import (
    SegmentSlateFramesWorkflow,
)

__all__ = ["FIT_STAGE", "SEGMENT_STAGE"]


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


_PROCESSOR: Any = None
_LOCK = threading.Lock()


def _segmenter(path: str):
    """``(image, prompt) -> [(mask, score)]`` at the port's threshold, on a
    processor of its own over the shared, already-loaded model."""
    global _PROCESSOR  # pylint: disable=global-statement
    with _LOCK:
        if _PROCESSOR is None:
            # pylint: disable-next=import-outside-toplevel,import-error
            from sam3.model.sam3_image_processor import Sam3Processor

            _PROCESSOR = Sam3Processor(
                get_segmenter(path).model, confidence_threshold=SHADOW_SAM_THRESHOLD
            )

    adapter = ScoredSam3Adapter(_PROCESSOR)
    encoded: list = [None, None]  # (image, its encoder state)

    def segment(image, prompt):
        # One encoder pass per image, every prompt asked of it (review of
        # #261: "white board" wins 253 of the pool's 258 frames, so nearly
        # every frame asks both). The adapter takes BGR, as the frames it was
        # written for.
        if encoded[0] is not image:
            encoded[:] = [image, adapter.encoded(image[:, :, ::-1])]
        return adapter.prompted(encoded[1], prompt)

    return segment


# pylint: disable-next=protected-access
_SAM3 = HeadtailPredictActivities(
    store_factory=_store, sam3_checkpoint=headtail_stage._sam3_checkpoint
)._verified_sam3_checkpoint

_ACTIVITIES = ShadowCalibrationActivities(
    store_factory=_store,
    sam3_checkpoint=_SAM3,
    cuda_available=cuda_available,
    segmenter=_segmenter,
)

SEGMENT_STAGE = Stage(
    name="shadow_segment",
    role=ROLE_GPU,
    workflows=[SegmentSlateFramesWorkflow],
    activities=[_ACTIVITIES.segment_slate_frame],
)
FIT_STAGE = Stage(
    name="shadow_fit",
    role=ROLE_LIGHT,
    workflows=[FitShadowCalibrationWorkflow],
    activities=[_ACTIVITIES.fit_shadow_calibration],
)
