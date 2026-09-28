"""Stage 13, slate laser calibration, as a processor stage.

On the light role, as in v1 (fishsense-lite@77e8f8e5 roles.py
`LIGHT_WORKFLOWS`): the fit reads already-labelled points and holds no image
bytes, so it must not wait behind the per-image role's memory cap -- on
2026-09-04 two consecutive v1 calibrations expired on ScheduleToStart behind
one preprocess.
"""

from fishsense_services_processor.laser_calibration.activities import (
    perform_laser_calibration,
)
from fishsense_services_processor.laser_calibration.workflow import (
    PerformLaserCalibrationWorkflow,
)
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

STAGE = Stage(
    name="laser_calibration",
    role=ROLE_LIGHT,
    workflows=[PerformLaserCalibrationWorkflow],
    activities=[perform_laser_calibration],
)
