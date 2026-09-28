"""Stage 0.1, laser preprocessing, as a processor stage.

On the per-image role: each activity decodes a full-res `.ORF` (1-3 GB peak),
which is what that role's cap of 2 is for (fishsense-lite@77e8f8e5 roles.py
`CPU_WORKFLOWS`).
"""

from fishsense_services_processor.laser_preprocess.activities import (
    preprocess_laser_image,
)
from fishsense_services_processor.laser_preprocess.workflow import (
    PreprocessLaserImagesWorkflow,
)
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage

STAGE = Stage(
    name="laser_preprocess",
    role=ROLE_PER_IMAGE,
    workflows=[PreprocessLaserImagesWorkflow],
    activities=[preprocess_laser_image],
)
