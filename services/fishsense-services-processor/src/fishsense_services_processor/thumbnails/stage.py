"""The thumbnail stage, on the per-image role: scaling a render needs its
memory, not a GPU. Nothing loads at import."""

from __future__ import annotations

from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage
from fishsense_services_processor.thumbnails.activities import ThumbnailActivities
from fishsense_services_processor.thumbnails.workflow import MakeThumbnailsWorkflow

_ACTIVITIES = ThumbnailActivities()

STAGE = Stage(
    name="thumbnails",
    role=ROLE_PER_IMAGE,
    workflows=[MakeThumbnailsWorkflow],
    activities=[_ACTIVITIES.make_thumbnail],
)
