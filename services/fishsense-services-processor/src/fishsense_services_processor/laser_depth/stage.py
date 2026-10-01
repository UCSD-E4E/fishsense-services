"""Laser depth, as a processor stage.

On the light role, as in v1 (fishsense-lite@77e8f8e5 roles.py
`LIGHT_WORKFLOWS`): it holds no image bytes -- labels in, numpy, rows out --
so it must not wait behind the per-image role's memory cap.
"""

from fishsense_services_processor.laser_depth.activities import compute_laser_depths
from fishsense_services_processor.laser_depth.workflow import (
    ComputeLaserDepthsWorkflow,
)
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

STAGE = Stage(
    name="laser_depth",
    role=ROLE_LIGHT,
    workflows=[ComputeLaserDepthsWorkflow],
    activities=[compute_laser_depths],
)
