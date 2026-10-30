"""The laser-color backfill as a processor stage on the light role: a render
read and one pixel neighborhood classified per dot, no model, no GPU. Run by
hand (`laser_colour_backfill.run` in the orchestrator)."""

from __future__ import annotations

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.laser_colour_backfill.activities import (
    LaserColourActivities,
)
from fishsense_services_processor.laser_colour_backfill.workflow import (
    ReadLaserColoursWorkflow,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

__all__ = ["STAGE"]

_ACTIVITIES = LaserColourActivities(
    store_factory=lambda: ProcessorObjectStore.from_settings(ObjectStoreConnection()),
)

STAGE = Stage(
    name="laser_colour_backfill",
    role=ROLE_LIGHT,
    workflows=[ReadLaserColoursWorkflow],
    activities=[_ACTIVITIES.read_laser_colour],
)
