"""Stage 14 (measure fish), as a processor stage.

On the light role, as in v1 (fishsense-lite@77e8f8e5 roles.py
`LIGHT_WORKFLOWS`): labels in, numpy, lengths out; no image bytes.
"""

from fishsense_services_processor.measurement.activities import measure_fish
from fishsense_services_processor.measurement.workflow import MeasureFishWorkflow
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

STAGE = Stage(
    name="measurement",
    role=ROLE_LIGHT,
    workflows=[MeasureFishWorkflow],
    activities=[measure_fish],
)
