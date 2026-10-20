"""The render stage, on the per-image role: decoding a raw needs its memory,
not a GPU. Nothing loads at import."""

from __future__ import annotations

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.headtail_preprocess.activities import (
    rectify_and_encode_jpeg,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage
from fishsense_services_processor.renders.activities import RenderActivities
from fishsense_services_processor.renders.workflow import RenderCapturesWorkflow

_ACTIVITIES = RenderActivities(
    store_factory=lambda: ProcessorObjectStore.from_settings(ObjectStoreConnection()),
    render_jpeg=rectify_and_encode_jpeg,
)

STAGE = Stage(
    name="renders",
    role=ROLE_PER_IMAGE,
    workflows=[RenderCapturesWorkflow],
    activities=[_ACTIVITIES.render_capture],
)
