"""The laser point the three depth-using stages share (`laser_dots`): which
calibration a broken input blames, and the refusal vocabulary.

fishsense-core's `compute_laser_point` raises `ValueError` for a zero or
non-finite laser axis (the dive's laser calibration) and for a camera matrix
that is not 3x3, not finite or singular (its camera calibration). Each is the
dive's, not an image's, so each fails non-retryably -- named for the one
that is broken (review of #361: a bad camera matrix was reported as an
unusable laser calibration, sending an operator to the wrong one). These
drive core's real errors, so a change in how core signals either fails
here rather than slipping past the guard (review of #361: no test reached
it after test_laser_geometry.py went).
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser_depth import LaserDepthCapture, LaserDot
from fishsense_services_processor import laser_dots
from fishsense_services_processor.automatic_measurement.activities import (
    measure_automatic,
)
from fishsense_services_processor.laser_depth.activities import compute_laser_depths
from fishsense_services_processor.measurement.activities import measure_fish

K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
POSITION = (-0.03, -0.10, 0.0)
AXIS = (0.0, -0.02, 1.0)


@pytest.mark.parametrize(
    ("axis", "camera", "kind"),
    [
        ((0.0, 0.0, 0.0), K, "UnusableLaserCalibration"),
        ((float("nan"), 0.0, 1.0), K, "UnusableLaserCalibration"),
        (AXIS, [[0.0] * 3] * 3, "UnusableCameraCalibration"),
        (AXIS, [[float("nan")] * 3] * 3, "UnusableCameraCalibration"),
        (AXIS, [[3000.0, 0.0], [0.0, 3000.0]], "UnusableCameraCalibration"),
    ],
    ids=["zero-axis", "nan-axis", "singular-k", "nan-k", "2x2-k"],
)
def test_a_broken_input_blames_its_own_calibration(axis, camera, kind):
    with pytest.raises(ApplicationError) as e:
        laser_dots.laser_point((2000.0, 1500.0), POSITION, axis, camera, of="dive 1")
    assert (e.value.type, e.value.non_retryable) == (kind, True)
    assert "dive 1" in e.value.message


def test_a_good_input_is_cores_point():
    point = laser_dots.laser_point((2048.0, 1400.0), POSITION, AXIS, K, of="dive 1")
    assert point.is_measurable() and laser_dots.dot_refusal(point) is None


# -- each stage that triangulates a dot uses it ----------------------------------------


async def test_the_depth_stage_names_a_broken_laser_calibration():
    from fishsense_services_contracts.laser_depth import (
        ComputeLaserDepthsInput,
        LaserCalibrationGeometry,
    )

    payload = ComputeLaserDepthsInput(
        mission_id=uuid.uuid4(), camera_matrix=K,
        calibration=LaserCalibrationGeometry(laser_calibration_id=uuid.uuid4(),
                                             laser_position=POSITION,
                                             laser_axis=(0.0, 0.0, 0.0)),
        captures=[LaserDepthCapture(capture_id=uuid.uuid4(), laser_labels=[
            LaserDot(laser_label_id=uuid.uuid4(), x=2000.0, y=1500.0)])],
    )  # fmt: skip
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(compute_laser_depths, payload)
    assert e.value.type == "UnusableLaserCalibration"


async def test_stage_14_names_a_broken_camera_calibration():
    from tests.test_measure_fish import _capture, _input, _laser_calibration

    payload = _input(_laser_calibration(), [_capture((2000.0, 1500.0), (1900.0, 1500.0),
                                                     (2100.0, 1500.0))])  # fmt: skip
    payload = payload.model_copy(update={"camera_matrix": ((0.0,) * 3,) * 3})
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(measure_fish, payload)
    assert e.value.type == "UnusableCameraCalibration"


async def test_the_automatic_stage_names_a_broken_laser_calibration():
    from tests.test_automatic_calibration_and_measure import _capture, _measure_input

    payload = _measure_input(_capture((2000.0, 1500.0)))
    payload = payload.model_copy(update={"laser_axis": [0.0, 0.0, 0.0]})
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(measure_automatic, payload)
    assert e.value.type == "UnusableLaserCalibration"


async def test_the_automatic_stage_stores_a_non_finite_depth_as_such(monkeypatch):
    """A finite, positive depth whose range or residual is not finite: the
    solve broke down, so `non_finite_depth`. The automatic stage stored it as
    `non_positive_depth`, misnaming a positive depth (review of #361); 0071's
    CHECK names the true reason."""
    from fishsense_core.world_point import LaserPoint

    from fishsense_services_processor.automatic_measurement import activities as auto
    from tests.test_automatic_calibration_and_measure import _capture, _measure_input

    broken = LaserPoint(depth_m=3.2, range_m=float("nan"), residual_m=0.0,
                        parallax_rad=0.05)  # fmt: skip
    monkeypatch.setattr(auto, "laser_point", lambda *a, **k: broken)

    result = await ActivityEnvironment().run(
        measure_automatic, _measure_input(_capture((2000.0, 1500.0)))
    )

    (length,) = result.lengths
    assert (length.refusal, length.depth_m, length.length_m) == (
        "non_finite_depth", 3.2, None,
    )  # fmt: skip
