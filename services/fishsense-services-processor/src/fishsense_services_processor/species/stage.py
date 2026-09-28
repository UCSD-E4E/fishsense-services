"""Stage 2, species preprocessing, as a processor stage.

On the per-image role: each activity decodes a full-res `.ORF` (1-3 GB), so it
runs under the role whose concurrency is a memory ceiling (v1's cpu queue,
fishsense-lite@77e8f8e5 roles.py).
"""

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage
from fishsense_services_processor.species.activities import SpeciesImageActivities
from fishsense_services_processor.species.workflow import (
    PreprocessSpeciesImagesWorkflow,
)

_activities = SpeciesImageActivities(
    store_factory=lambda: ProcessorObjectStore.from_settings(ObjectStoreConnection())
)

STAGE = Stage(
    name="species",
    role=ROLE_PER_IMAGE,
    workflows=[PreprocessSpeciesImagesWorkflow],
    activities=[_activities.preprocess_species_image],
)
