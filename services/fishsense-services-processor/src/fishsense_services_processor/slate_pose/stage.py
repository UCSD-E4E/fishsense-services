"""Calibration v4's slate pose as a processor stage on the GPU role, over the
shadow calibration's SAM wiring (its verified checkpoint, its 0.3-threshold
processor on the shared model). Read-only: run by the replay alone."""

from fishsense_services_processor.headtail_predict.predict import cuda_available
from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.shadow_calibration import wiring
from fishsense_services_processor.slate_pose.activities import SlatePoseActivities
from fishsense_services_processor.slate_pose.workflow import SlatePoseFramesWorkflow

_ACTIVITIES = SlatePoseActivities(
    store_factory=wiring._store,  # pylint: disable=protected-access
    sam3_checkpoint=wiring._SAM3,  # pylint: disable=protected-access
    cuda_available=cuda_available,
    segmenter=wiring._segmenter,  # pylint: disable=protected-access
)

STAGE = Stage(
    name="slate_pose",
    role=ROLE_GPU,
    workflows=[SlatePoseFramesWorkflow],
    activities=[_ACTIVITIES.slate_pose_frame],
)
