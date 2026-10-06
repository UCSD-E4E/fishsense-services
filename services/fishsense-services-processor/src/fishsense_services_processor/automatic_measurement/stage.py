"""Automatic lengths, as a processor stage: on the light role, as stage 14
(rows in, numpy, rows out; no image bytes)."""

from fishsense_services_processor.automatic_measurement.activities import (
    measure_automatic,
)
from fishsense_services_processor.automatic_measurement.workflow import (
    MeasureAutomaticWorkflow,
)
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

STAGE = Stage(
    name="automatic_measurement",
    role=ROLE_LIGHT,
    workflows=[MeasureAutomaticWorkflow],
    activities=[measure_automatic],
)
