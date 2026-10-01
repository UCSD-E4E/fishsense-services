"""Laser prediction as a processor stage.

On the GPU role: torch inference. The GPU queue means *prefer* a GPU -- when the
GPU Deployment can't start, a CPU-only one serves the same queue (orchestrator
`nrp.gpu_fallback`, fishsense-lite@77e8f8e5 roles.py `GPU_ACTIVITIES`).
"""

from fishsense_services_processor.laser_predict.activities import predict_laser_image
from fishsense_services_processor.laser_predict.workflow import (
    PredictLaserImagesWorkflow,
)
from fishsense_services_processor.registry import ROLE_GPU, Stage

STAGE = Stage(
    name="laser_predict",
    role=ROLE_GPU,
    workflows=[PredictLaserImagesWorkflow],
    activities=[predict_laser_image],
)
