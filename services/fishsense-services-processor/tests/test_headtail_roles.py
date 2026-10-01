"""The head/tail stages' roles.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_worker_roles.py (the head/tail rows): stage 5.1
decodes a full-res `.ORF`, so it is a per-image stage under that role's memory
cap; prediction runs torch on the GPU queue (or its CPU fallback). A stage on
the wrong role is silent: its child sits on a queue nobody serves.
"""

from fishsense_services_processor import registry
from fishsense_services_processor.headtail_predict.workflow import (
    PredictHeadtailImagesWorkflow,
)
from fishsense_services_processor.headtail_preprocess.workflow import (
    PreprocessHeadtailImagesWorkflow,
)


def _names(registration):
    return {a.__temporal_activity_definition.name for a in registration.activities}


def test_stage_5_1_is_a_per_image_stage():
    registration = registry.registration_for_role(registry.ROLE_PER_IMAGE)
    assert PreprocessHeadtailImagesWorkflow in registration.workflows
    assert "preprocess_headtail_image" in _names(registration)


def test_head_tail_prediction_is_a_gpu_stage():
    registration = registry.registration_for_role(registry.ROLE_GPU)
    assert PredictHeadtailImagesWorkflow in registration.workflows
    assert "predict_headtail_image" in _names(registration)


def test_neither_is_on_the_light_role():
    registration = registry.registration_for_role(registry.ROLE_LIGHT)
    assert not {
        PreprocessHeadtailImagesWorkflow,
        PredictHeadtailImagesWorkflow,
    } & set(registration.workflows)
