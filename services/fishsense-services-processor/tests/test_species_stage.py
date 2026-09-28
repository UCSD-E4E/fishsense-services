"""Stage 2 is a per-image stage.

v1 ran `PreprocessSpeciesImagesWorkflow` on its cpu queue
(fishsense-lite@77e8f8e5 roles.py): each activity decodes a full-res `.ORF`
(1-3 GB), so it belongs to the role whose concurrency is capped by memory.
"""

from fishsense_services_processor import registry
from fishsense_services_processor.registry import registration_for_role
from fishsense_services_processor.species.workflow import (
    PreprocessSpeciesImagesWorkflow,
)


def test_species_preprocessing_is_a_per_image_stage():
    registration = registration_for_role(registry.ROLE_PER_IMAGE)

    assert PreprocessSpeciesImagesWorkflow in registration.workflows
    assert {a.__temporal_activity_definition.name for a in registration.activities} >= {
        "preprocess_species_image"
    }


def test_it_is_served_by_no_other_role():
    for role in (registry.ROLE_LIGHT, registry.ROLE_GPU):
        assert PreprocessSpeciesImagesWorkflow not in (
            registration_for_role(role).workflows
        )
