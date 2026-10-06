"""The label-free calibration, as a processor stage (per-image role: it decodes
JPEGs; see `activities`)."""

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.automatic_calibration.activities import (
    AutomaticCalibrationActivities,
)
from fishsense_services_processor.automatic_calibration.workflow import (
    FitAutomaticCalibrationWorkflow,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage

_ACTIVITIES = AutomaticCalibrationActivities(
    store_factory=lambda: ProcessorObjectStore.from_settings(ObjectStoreConnection())
)

STAGE = Stage(
    name="automatic_calibration",
    role=ROLE_PER_IMAGE,
    workflows=[FitAutomaticCalibrationWorkflow],
    activities=[_ACTIVITIES.fit_automatic_calibration],
)
