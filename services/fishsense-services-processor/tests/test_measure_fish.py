"""Stage 14's processor activity: a length per capture, or why there is none.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_measure_fish_activity.py. v1's activity chose the
images, bound fish and wrote rows through the SDK, and its tests mocked all of
that; in v2 those decisions are the store's (fishsense-services-api
tests/test_measurement_store.py), and the processor only does the geometry.
What stays here is v1's synthetic-geometry happy path (a length within 1 mm of
the constructed fish) and the non-finite drop.

v2 changes, pinned:

* a non-finite length is a **refusal that comes back**, not a counter
  (`dropped_nan`) with nothing written -- the dive would stay in its cohort;
* a **zero** length (head and tail on one pixel) is a refusal too: v2's
  `measurements` refuses it (CHECK length_m > 0), where v1 stored it;
* the length is taken at the laser's depth **whatever its sign**, exactly as
  v1's stage 14 did (`isfinite` only). The depth stage gates on `depth_m > 0`
  and stage 14 never did; adding the gate here would change the measurement
  parity numbers (PLAN.md §6.2), so it is left for a deliberate decision.
"""

from __future__ import annotations

import importlib.metadata
from uuid import uuid4

import numpy as np
import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser_depth import LaserCalibrationGeometry, LaserDot
from fishsense_services_contracts.measurement import (
    HeadTail,
    MeasureFishCapture,
    MeasureFishInput,
)
from fishsense_services_processor.laser_geometry import compute_laser_point
from fishsense_services_processor.measurement.activities import (
    ALGORITHM,
    ALGORITHM_VERSION,
    measure_fish,
)

CAMERA_MATRIX = np.array(
    [
        [3000.0, 0.0, 2048.0],
        [0.0, 3000.0, 1536.0],
        [0.0, 0.0, 1.0],
    ]
)


def _project(point_camera: np.ndarray) -> tuple[float, float]:
    p = CAMERA_MATRIX @ point_camera
    return float(p[0] / p[2]), float(p[1] / p[2])


def _laser_calibration() -> LaserCalibrationGeometry:
    # Off-axis laser: origin offset in -x, axis tilted slightly in -y.
    axis = np.array([0.0, -0.02, 1.0])
    return LaserCalibrationGeometry(
        laser_calibration_id=uuid4(),
        laser_position=(-0.03, -0.10, 0.0),
        laser_axis=tuple(axis / np.linalg.norm(axis)),
    )


def _build_observation(calibration, head_world, tail_world, target_depth):
    """Project the head + tail + the laser's hit at `target_depth` into
    pixels with CAMERA_MATRIX. Head/tail must lie on the plane Z=target_depth
    so the depth-from-laser back-projection recovers them exactly."""
    o = np.asarray(calibration.laser_position, dtype=float)
    a = np.asarray(calibration.laser_axis, dtype=float)
    t = (target_depth - o[2]) / a[2]
    laser_hit = o + t * a
    return _project(head_world), _project(tail_world), _project(laser_hit)


def _capture(laser_pix, head_pix, tail_pix) -> MeasureFishCapture:
    return MeasureFishCapture(
        capture_id=uuid4(),
        species_label_id=uuid4(),
        laser=LaserDot(laser_label_id=uuid4(), x=laser_pix[0], y=laser_pix[1]),
        head_tail=HeadTail(
            head_tail_label_id=uuid4(),
            head_x=head_pix[0],
            head_y=head_pix[1],
            tail_x=tail_pix[0],
            tail_y=tail_pix[1],
        ),
    )


def _input(calibration, captures) -> MeasureFishInput:
    return MeasureFishInput(
        dive_id=uuid4(),
        camera_matrix=tuple(tuple(row) for row in CAMERA_MATRIX),
        calibration=calibration,
        captures=captures,
    )


async def _run(payload):
    return await ActivityEnvironment().run(measure_fish, payload)


async def test_measures_one_fish_end_to_end():
    le = _laser_calibration()
    target_depth = 1.20  # meters

    head_world = np.array([-0.10, 0.00, target_depth])
    tail_world = np.array([0.20, 0.00, target_depth])
    expected_length = float(np.linalg.norm(head_world - tail_world))

    head_pix, tail_pix, laser_pix = _build_observation(
        le, head_world, tail_world, target_depth
    )
    capture = _capture(laser_pix, head_pix, tail_pix)

    result = await _run(_input(le, [capture]))

    (length,) = result.captures
    # Measurement length is within 1 mm of ground truth.
    assert abs(length.length_m - expected_length) < 1e-3
    assert length.depth_m == pytest.approx(target_depth, abs=1e-3)
    assert length.refusal is None
    # Echoes what it measured from, so persistence binds exactly these.
    assert length.capture_id == capture.capture_id
    assert length.species_label_id == capture.species_label_id
    assert length.laser == capture.laser
    assert length.head_tail == capture.head_tail


async def test_measures_every_capture_in_order():
    le = _laser_calibration()
    head_pix, tail_pix, laser_pix = _build_observation(
        le, np.array([-0.10, 0.00, 1.20]), np.array([0.20, 0.00, 1.20]), 1.20
    )
    captures = [_capture(laser_pix, head_pix, tail_pix) for _ in range(3)]

    result = await _run(_input(le, captures))

    assert [c.capture_id for c in result.captures] == [c.capture_id for c in captures]
    assert all(c.length_m is not None for c in result.captures)


async def test_a_zero_length_is_refused_not_measured():
    """Head and tail on one pixel. v1 stored 0; v2's measurements refuse it
    (CHECK length_m > 0), and an insert that fails would fail the whole dive,
    hourly. It comes back as a refusal the orchestrator records."""
    le = _laser_calibration()
    _, _, laser_pix = _build_observation(
        le, np.array([0.0, 0.0, 1.2]), np.array([0.0, 0.0, 1.2]), 1.20
    )
    same = (2000.0, 1500.0)

    result = await _run(_input(le, [_capture(laser_pix, same, same)]))

    (length,) = result.captures
    assert length.length_m is None
    assert length.refusal == "zero_length"
    assert length.depth_m == pytest.approx(1.20, abs=1e-3)


async def test_a_non_finite_length_is_refused_not_dropped():
    """v1's `dropped_nan`: the triangulation's degenerate case gives a NaN
    point, and every length at a NaN depth is NaN. v1 dropped it and wrote
    nothing, so the image stayed unmeasured and the dive in its cohort."""
    # A laser through the camera centre along the optical axis, and a dot on
    # the principal point: the two rays are the same line, and have no
    # closest point.
    le = LaserCalibrationGeometry(
        laser_calibration_id=uuid4(),
        laser_position=(0.0, 0.0, 0.0),
        laser_axis=(0.0, 0.0, 1.0),
    )
    capture = _capture((2048.0, 1536.0), (1900.0, 1500.0), (2100.0, 1500.0))
    # Guard the guard: the geometry really is the kernel's NaN case.
    assert np.isnan(compute_laser_point(capture.laser, le, CAMERA_MATRIX).depth_m)

    result = await _run(_input(le, [capture]))

    (length,) = result.captures
    assert length.length_m is None
    assert length.depth_m is None
    assert length.refusal == "non_finite_length"


async def test_a_length_at_a_negative_depth_is_still_measured_as_v1_did():
    """Parity, deliberately. v1's stage 14 guarded only on `isfinite`, so a
    dot on the wrong side of the principal point measured at -Z, and the
    length at -Z equals the length at +Z. The depth stage refuses such a dot;
    stage 14 adding the same gate would change which frames are measured, and
    so the §6.2 parity numbers -- a decision, not a port."""
    le = _laser_calibration()
    head_pix, tail_pix, _ = _build_observation(
        le, np.array([-0.10, 0.00, 1.20]), np.array([0.20, 0.00, 1.20]), 1.20
    )
    # Laser offset is -x, so a real dot lands left of the principal point; a
    # label to the right of it inverts the solve.
    capture = _capture((3000.0, 1600.0), head_pix, tail_pix)

    result = await _run(_input(le, [capture]))

    (length,) = result.captures
    assert length.depth_m < 0
    assert length.length_m > 0
    assert length.refusal is None


async def test_the_result_names_its_algorithm_version_and_core():
    """0013 requires algorithm, algorithm_version and core_version of every
    server measurement; v1 recorded none of them."""
    result = await _run(_input(_laser_calibration(), []))

    assert result.algorithm == ALGORITHM
    assert result.algorithm_version == ALGORITHM_VERSION
    assert result.core_version == importlib.metadata.version("fishsense-core")
    assert result.captures == []
