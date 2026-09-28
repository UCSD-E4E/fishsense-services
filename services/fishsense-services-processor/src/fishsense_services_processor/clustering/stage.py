"""Stage 1, dive-frame clustering, as a processor stage.

On the light role: clustering is maths on capture timestamps and holds no
image bytes, so it must not wait behind the per-image role's memory cap (v1's
light queue, fishsense-lite@77e8f8e5 roles.py `LIGHT_WORKFLOWS`).
"""

from fishsense_services_processor.clustering.activities import cluster_dive_frames
from fishsense_services_processor.clustering.workflow import (
    DiveFrameClusteringWorkflow,
)
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

STAGE = Stage(
    name="clustering",
    role=ROLE_LIGHT,
    workflows=[DiveFrameClusteringWorkflow],
    activities=[cluster_dive_frames],
)
