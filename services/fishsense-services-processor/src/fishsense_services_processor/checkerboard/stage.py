"""Checkerboard calibration and lattice verification, as a processor stage.

On the per-image role, as in v1 (fishsense-lite@77e8f8e5 roles.py: v1's CPU
queue, "not light: it decodes .ORF files"). Each frame is a rawpy decode
peaking at 1-3 GB, the memory ceiling behind the role's cap of 2. The fit runs
here too: an activity runs on its workflow's queue, and it is milliseconds.
"""

from fishsense_services_processor.checkerboard.activities import (
    detect_checkerboard_laser_point,
    fit_checkerboard_laser_extrinsics,
    render_checkerboard_lattice,
)
from fishsense_services_processor.checkerboard.workflows import (
    PerformCheckerboardCalibrationWorkflow,
    VerifyCheckerboardLatticeWorkflow,
)
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage

STAGE = Stage(
    name="checkerboard",
    role=ROLE_PER_IMAGE,
    workflows=[
        PerformCheckerboardCalibrationWorkflow,
        VerifyCheckerboardLatticeWorkflow,
    ],
    activities=[
        detect_checkerboard_laser_point,
        fit_checkerboard_laser_extrinsics,
        render_checkerboard_lattice,
    ],
)
