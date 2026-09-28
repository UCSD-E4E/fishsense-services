"""The slate and calibration stages serve v1's roles.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_worker_roles.py (the stage-9, stage-13,
checkerboard and lattice rows) and src/.../roles.py's lists. v1's reasons:

* stage 9, checkerboard calibration and lattice verification each decode a
  full-resolution `.ORF` per frame (1-3 GB), so they run on the per-image
  role, whose cap of 2 is a memory ceiling;
* stage 13 reads already-labelled points and holds no image bytes, so it runs
  on the light role -- on 2026-09-04 two consecutive v1 calibrations expired on
  ScheduleToStart queued behind one preprocess.
"""

from fishsense_services_processor import registry
from fishsense_services_processor.checkerboard.activities import (
    detect_checkerboard_laser_point,
    fit_checkerboard_laser_extrinsics,
    render_checkerboard_lattice,
)
from fishsense_services_processor.checkerboard.workflows import (
    PerformCheckerboardCalibrationWorkflow,
    VerifyCheckerboardLatticeWorkflow,
)
from fishsense_services_processor.laser_calibration.activities import (
    perform_laser_calibration,
)
from fishsense_services_processor.laser_calibration.workflow import (
    PerformLaserCalibrationWorkflow,
)
from fishsense_services_processor.registry import registration_for_role
from fishsense_services_processor.slate_preprocess.activities import (
    preprocess_slate_image,
)
from fishsense_services_processor.slate_preprocess.workflow import (
    PreprocessSlateImagesWorkflow,
)


def test_stage_13_is_a_light_stage():
    light = registration_for_role(registry.ROLE_LIGHT)
    assert PerformLaserCalibrationWorkflow in light.workflows
    assert perform_laser_calibration in light.activities


def test_the_decoding_stages_are_per_image():
    per_image = registration_for_role(registry.ROLE_PER_IMAGE)
    for workflow in (
        PreprocessSlateImagesWorkflow,
        PerformCheckerboardCalibrationWorkflow,
        VerifyCheckerboardLatticeWorkflow,
    ):
        assert workflow in per_image.workflows
    for activity in (
        preprocess_slate_image,
        detect_checkerboard_laser_point,
        fit_checkerboard_laser_extrinsics,
        render_checkerboard_lattice,
    ):
        assert activity in per_image.activities


def test_none_of_them_is_on_the_gpu_role():
    """v1's slate predictor was the only GPU stage here, and it is retired
    (2026-08-03): nothing of this slice may land on a GPU pod."""
    gpu = registration_for_role(registry.ROLE_GPU)
    ours = {
        PreprocessSlateImagesWorkflow,
        PerformLaserCalibrationWorkflow,
        PerformCheckerboardCalibrationWorkflow,
        VerifyCheckerboardLatticeWorkflow,
    }
    assert not ours & set(gpu.workflows)
