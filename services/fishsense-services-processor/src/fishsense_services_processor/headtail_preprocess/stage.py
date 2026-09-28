"""Stage 5.1, head/tail preprocess, as a processor stage.

On the per-image role: each activity decodes a full-res `.ORF` (1-3 GB peak),
so it sits under that role's memory cap (v1's CPU data-worker,
fishsense-lite@77e8f8e5 roles.py). The object store is read from
`FISHSENSE_OBJECT_STORE_*` on the first activity, not here: the registry
imports every stage wherever it runs.
"""

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.headtail_preprocess.activities import (
    HeadtailPreprocessActivities,
)
from fishsense_services_processor.headtail_preprocess.workflow import (
    PreprocessHeadtailImagesWorkflow,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


_ACTIVITIES = HeadtailPreprocessActivities(store_factory=_store)

STAGE = Stage(
    name="headtail_preprocess",
    role=ROLE_PER_IMAGE,
    workflows=[PreprocessHeadtailImagesWorkflow],
    activities=[_ACTIVITIES.preprocess_headtail_image],
)
