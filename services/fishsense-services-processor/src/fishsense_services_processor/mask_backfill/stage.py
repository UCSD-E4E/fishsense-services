"""The mask backfill on the GPU role, over the automatic track's and the
head/tail stage's own SAM 3.1 seams (the model loads once, shared)."""

from __future__ import annotations

from fishsense_core.fish.fish_at_dot import Sam3Segmenter
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.headtail_predict import stage as headtail_stage
from fishsense_services_processor.headtail_predict.activities import (
    HeadtailPredictActivities,
)
from fishsense_services_processor.headtail_predict.predict import (
    cuda_available,
    get_segmenter,
)
from fishsense_services_processor.mask_backfill.activities import (
    MaskBackfillActivities,
)
from fishsense_services_processor.mask_backfill.workflow import BackfillMasksWorkflow
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, Stage


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


# pylint: disable-next=protected-access
_SAM3 = HeadtailPredictActivities(
    store_factory=_store, sam3_checkpoint=headtail_stage._sam3_checkpoint
)._verified_sam3_checkpoint

_ACTIVITIES = MaskBackfillActivities(
    store_factory=_store,
    sam3_checkpoint=_SAM3,
    cuda_available=cuda_available,
    # Core's SAM 3.1 seam serves both paths: scored for the automatic gate,
    # plain for head/tail.
    segmenter=lambda path: Sam3Segmenter(get_segmenter(path)),
)

STAGE = Stage(
    name="mask_backfill",
    role=ROLE_GPU,
    workflows=[BackfillMasksWorkflow],
    activities=[_ACTIVITIES.backfill_prediction_mask],
)
