"""Stage 9, slate preprocessing, as a processor stage.

On the per-image role, as in v1 (fishsense-lite@77e8f8e5 roles.py, the CPU
queue): each frame is a full-resolution `.ORF` decode peaking at 1-3 GB.
"""

from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage
from fishsense_services_processor.slate_preprocess.activities import (
    preprocess_slate_image,
)
from fishsense_services_processor.slate_preprocess.workflow import (
    PreprocessSlateImagesWorkflow,
)

STAGE = Stage(
    name="slate_preprocess",
    role=ROLE_PER_IMAGE,
    workflows=[PreprocessSlateImagesWorkflow],
    activities=[preprocess_slate_image],
)
