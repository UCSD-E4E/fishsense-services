"""The laser-geometry kernel shared by stage 14 and the depth stage.

Ported from fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/tests/test_laser_geometry.py.
Test names, bodies and reasons are v1's; the one v2 adaptation is the
signature: the processor is handed the camera matrix from the processing
contract, not v1's SDK `CameraIntrinsics`, so the kernel takes the matrix.

Stage 14 has always computed the distance to the laser dot on its way to a
fish length and then discarded it. Now that the depth is stored per image,
two callers need the same projection, and a second transcription of it is
exactly the kind of copy that drifts — the sign-convention investigation
behind `test_stage14_pipeline_sign_consistency.py` is what that costs. So the
projection lives in one module and both callers import it.

These tests pin the two properties a second caller can get wrong: that
`depth_m` is the Z component (not the Euclidean distance) and that
`measure_length_at_depth` still composes into exactly the length stage 14
produced before the extraction.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from fishsense_core.world_point import WorldPointHandler

from fishsense_services_processor.laser_geometry import (
    compute_laser_point,
    measure_length_at_depth,
)


def _stub(**attrs) -> SimpleNamespace:
    """Minimal stand-in for the SDK label / intrinsics / extrinsics models —
    these functions only ever read a few attributes off whatever they are
    handed.

    `SimpleNamespace` rather than a class assigning `self.__dict__`: the
    latter reads identically at runtime but is opaque to static analysis, so
    every attribute access became a pylint `no-member`.
    """
    return SimpleNamespace(**attrs)


def _intrinsics(fx=2000.0, fy=2000.0, cx=1000.0, cy=750.0):
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])


def _extrinsics(position=(0.104, 0.0, 0.0), axis=(0.0, 0.0, 1.0)):
    return _stub(laser_position=np.array(position), laser_axis=np.array(axis))


def test_depth_is_the_z_component_of_the_projected_laser_point():
    """The number stage 14 back-projects head and tail against. Pinned
    against the kernel directly so the extraction cannot quietly substitute
    the slant distance, which is a different — and larger — number."""
    intrinsics, extrinsics = _intrinsics(), _extrinsics()
    laser_label = _stub(x=1200.0, y=800.0)

    handler = WorldPointHandler(np.linalg.inv(intrinsics))
    expected = handler.compute_world_point_from_laser(
        extrinsics.laser_position,
        extrinsics.laser_axis,
        np.array([laser_label.x, laser_label.y]),
    )

    point = compute_laser_point(laser_label, extrinsics, intrinsics)

    assert point.depth_m == pytest.approx(float(expected[2]))


def test_range_is_the_euclidean_distance_to_the_dot():
    intrinsics, extrinsics = _intrinsics(), _extrinsics()
    laser_label = _stub(x=1200.0, y=800.0)

    handler = WorldPointHandler(np.linalg.inv(intrinsics))
    expected = handler.compute_world_point_from_laser(
        extrinsics.laser_position,
        extrinsics.laser_axis,
        np.array([laser_label.x, laser_label.y]),
    )

    point = compute_laser_point(laser_label, extrinsics, intrinsics)

    assert point.range_m == pytest.approx(float(np.linalg.norm(expected)))


def test_range_exceeds_depth_for_an_off_axis_dot():
    """The two are only equal on the optical axis. A caller that treats them
    as interchangeable is wrong by the off-axis angle, and silently so."""
    intrinsics, extrinsics = _intrinsics(), _extrinsics()
    point = compute_laser_point(_stub(x=1600.0, y=1200.0), extrinsics, intrinsics)

    assert point.range_m > point.depth_m


def test_range_and_depth_agree_with_the_pixels_off_axis_angle():
    """`range_m / depth_m` is fixed by the pixel alone: the recovered point
    lies on the camera ray through it, so the ratio is |K^-1·[x,y,1]| over its
    Z component, whatever depth the laser happens to put it at. Pinning the
    ratio rather than either number checks the two are consistent without
    re-deriving the triangulation."""
    intrinsics, extrinsics = _intrinsics(), _extrinsics()
    laser_label = _stub(x=1600.0, y=1200.0)

    direction = np.linalg.inv(intrinsics) @ np.array(
        [laser_label.x, laser_label.y, 1.0]
    )
    expected_ratio = float(np.linalg.norm(direction) / direction[2])

    point = compute_laser_point(laser_label, extrinsics, intrinsics)

    assert point.range_m / point.depth_m == pytest.approx(expected_ratio)


def test_a_non_unit_laser_axis_does_not_change_the_answer():
    """Pins a guarantee we now depend on the dependency for.

    Until fishsense-core 3.0.0 the kernel silently assumed ‖axis‖ = 1 and
    returned garbage otherwise — doubling a unit axis flipped the depth
    negative. 3.0.0 normalises natively (we reported it), so this is no longer
    our job; it stays as a test because a regression there would rescale every
    depth and every fish length in silence, and nothing else here would catch
    it.
    """
    intrinsics = _intrinsics()
    unit = compute_laser_point(
        _stub(x=1200.0, y=800.0), _extrinsics(axis=(0.0, 0.0, 1.0)), intrinsics
    )

    for scale in (2.0, 0.5, 17.3):
        scaled = compute_laser_point(
            _stub(x=1200.0, y=800.0),
            _extrinsics(axis=(0.0, 0.0, scale)),
            intrinsics,
        )
        assert scaled.depth_m == pytest.approx(unit.depth_m, rel=1e-6)
        assert scaled.range_m == pytest.approx(unit.range_m, rel=1e-6)


def test_a_zero_length_axis_raises():
    """A calibration row with no direction cannot place a dot anywhere, and
    the kernel now says so instead of answering with a plausible ~1 cm depth.

    Left to propagate rather than caught and counted: the axis belongs to the
    dive's calibration, not to one image, so every image in the dive is
    equally unprocessable — failing the activity is the honest blast radius,
    and matches how a missing `LaserExtrinsics` row is already handled.
    """
    with pytest.raises(ValueError, match="laser_axis"):
        compute_laser_point(
            _stub(x=1200.0, y=800.0),
            _extrinsics(axis=(0.0, 0.0, 0.0)),
            _intrinsics(),
        )


def test_residual_is_near_zero_for_a_consistent_dot():
    """The dot really is on the laser ray, so the two rays meet: the
    closest-approach distance collapses to the float32 noise floor."""
    intrinsics = _intrinsics()
    extrinsics = _extrinsics()
    origin = np.asarray(extrinsics.laser_position, dtype=float)
    axis = np.asarray(extrinsics.laser_axis, dtype=float)
    truth = origin + 1.5 * axis / np.linalg.norm(axis)
    projected = intrinsics @ truth
    label = _stub(x=projected[0] / projected[2], y=projected[1] / projected[2])

    point = compute_laser_point(label, extrinsics, intrinsics)

    assert point.depth_m == pytest.approx(truth[2], rel=1e-3)
    assert point.residual_m < 1e-5


def test_residual_grows_when_the_dot_cannot_be_on_the_laser():
    """A dot displaced *across* the laser's epipolar line cannot lie on the
    laser at any depth, and the residual says so — this is the signal worth
    recording per image."""
    intrinsics = _intrinsics()
    extrinsics = _extrinsics()
    origin = np.asarray(extrinsics.laser_position, dtype=float)
    axis = np.asarray(extrinsics.laser_axis, dtype=float)
    truth = origin + 1.5 * axis / np.linalg.norm(axis)
    projected = intrinsics @ truth
    x, y = projected[0] / projected[2], projected[1] / projected[2]

    # The laser is offset in x, so its epipolar line runs horizontally here;
    # displace in y to move across it.
    off = compute_laser_point(_stub(x=x, y=y + 150.0), extrinsics, intrinsics)

    assert off.residual_m > 1e-3


def test_residual_is_blind_along_the_epipolar_line():
    """The limit that stops the residual being a sufficient check.

    Sliding the dot *along* the laser's epipolar line moves the recovered
    depth a long way while the rays still meet, so the residual stays at the
    noise floor. Pinned so nobody later promotes this number into a
    correctness gate on its own — the positive-depth and plausible-range
    checks are not redundant with it.
    """
    intrinsics = _intrinsics()
    extrinsics = _extrinsics()
    origin = np.asarray(extrinsics.laser_position, dtype=float)
    axis = np.asarray(extrinsics.laser_axis, dtype=float)
    truth = origin + 1.5 * axis / np.linalg.norm(axis)
    projected = intrinsics @ truth
    x, y = projected[0] / projected[2], projected[1] / projected[2]

    # The laser is offset purely in +x with an axis parallel to the optical
    # axis, so its epipolar line here is the horizontal y = const.
    along = compute_laser_point(_stub(x=x - 100.0, y=y), extrinsics, intrinsics)
    across = compute_laser_point(_stub(x=x, y=y + 100.0), extrinsics, intrinsics)

    # Same 100 px of error, three orders of magnitude apart in what the
    # residual reports — and the direction it cannot see is the one that
    # wrecks the depth.
    assert along.residual_m < across.residual_m / 100.0
    assert abs(along.depth_m - truth[2]) > 0.1, "yet the depth moved a lot"


def test_length_at_depth_matches_the_pre_extraction_composition():
    """Byte-for-byte the sequence stage 14 ran inline: project the laser for a
    depth, back-project head and tail at that depth, take the norm."""
    intrinsics, extrinsics = _intrinsics(), _extrinsics()
    laser_label = _stub(x=1200.0, y=800.0)
    headtail_label = _stub(head_x=900.0, head_y=700.0, tail_x=1400.0, tail_y=760.0)

    k_inv = np.linalg.inv(intrinsics)
    handler = WorldPointHandler(k_inv)
    laser3d = handler.compute_world_point_from_laser(
        extrinsics.laser_position,
        extrinsics.laser_axis,
        np.array([laser_label.x, laser_label.y]),
    )
    depth = float(laser3d[2])
    head3d = handler.compute_world_point_from_depth(
        np.array([headtail_label.head_x, headtail_label.head_y]), depth
    )
    tail3d = handler.compute_world_point_from_depth(
        np.array([headtail_label.tail_x, headtail_label.tail_y]), depth
    )
    expected = float(np.linalg.norm(head3d - tail3d))

    point = compute_laser_point(laser_label, extrinsics, intrinsics)
    actual = measure_length_at_depth(headtail_label, point.depth_m, intrinsics)

    assert actual == pytest.approx(expected)


def test_geometry_with_no_intersection_yields_a_non_positive_depth():
    """How failure actually presents, which is not how it reads.

    A laser axis parallel to the image plane never meets the camera ray, and
    the kernel answers with the origin rather than NaN or an exception. It is
    just as willing to return a *negative* depth when the dot sits on the
    wrong side of the principal point for the laser's offset — geometrically
    impossible for a real observation.

    Neither case is caught by an `isfinite` check, which is what stage 14
    guards its length with. And a length survives a negative depth intact:
    head and tail both scale by the same factor, so the norm of their
    difference comes out positive and plausible. `compute_laser_depths_activity`
    therefore gates on `depth_m > 0` rather than on finiteness.
    """
    intrinsics = _intrinsics()

    parallel = compute_laser_point(
        _stub(x=1200.0, y=800.0), _extrinsics(axis=(1.0, 0.0, 0.0)), intrinsics
    )
    assert parallel.depth_m <= 0.0
    assert np.isfinite(parallel.depth_m), "not NaN — an isfinite guard misses it"

    # Laser offset along +x, so a real dot always lands right of the principal
    # point. A label to the left of it inverts the solve.
    wrong_side = compute_laser_point(_stub(x=800.0, y=700.0), _extrinsics(), intrinsics)
    assert wrong_side.depth_m < 0.0
    assert np.isfinite(wrong_side.depth_m)


def test_a_length_survives_a_negative_depth_unchanged():
    """The reason the guard belongs on the depth and not on the length: the
    back-projection is linear in depth, so flipping its sign moves head and
    tail together and leaves their separation identical. An impossible
    geometry produces a perfectly ordinary-looking measurement."""
    intrinsics = _intrinsics()
    headtail_label = _stub(head_x=900.0, head_y=700.0, tail_x=1400.0, tail_y=760.0)

    positive = measure_length_at_depth(headtail_label, 1.5, intrinsics)
    negative = measure_length_at_depth(headtail_label, -1.5, intrinsics)

    assert negative == pytest.approx(positive)
