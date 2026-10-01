"""Unit tests for `calibration_geometry` — the target-agnostic half of stage 13.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_calibration_geometry.py.

Stage 13 fits laser extrinsics from observations of a dot lying on a plane of
known geometry. The *plane* is all the calibration target ever contributes: once
you have a normal and a point on it, recovering the dot's 3-D position is
identical whether the target was a dive slate, a checkerboard, or anything else
planar.

These tests pin that seam, because a second producer (checkerboard laser
calibration, `docs/plans/checkerboard-laser-calibration.md`) is about to be
built on it and the alternative is a second transcription of the projection —
which is exactly the drift `laser_geometry` was extracted to prevent.

Conventions match `test_perform_laser_calibration_activity`: +Z is depth,
projection is a plain pinhole through `CAMERA_MATRIX`, and `solvePnP` is given
zero distortion because the pipeline only ever feeds it rectified imagery.
"""

from __future__ import annotations

import cv2
import numpy as np

from fishsense_services_processor.calibration.geometry import (
    Plane,
    laser_point_on_plane,
    plane_from_correspondences,
)

CAMERA_MATRIX = np.array(
    [
        [3000.0, 0.0, 2048.0],
        [0.0, 3000.0, 1536.0],
        [0.0, 0.0, 1.0],
    ]
)

# A deliberately tilted pose. A fronto-parallel plane would hide a whole class
# of sign and transpose errors, because its normal is +Z whatever you do to the
# rotation.
_RVEC = np.array([0.12, -0.20, 0.05])
_TVEC = np.array([-0.06, 0.03, 1.40])


def _pose() -> tuple[np.ndarray, np.ndarray]:
    rotation, _ = cv2.Rodrigues(_RVEC)
    return rotation, _TVEC


def _to_camera(body_xy: np.ndarray) -> np.ndarray:
    """Lift planar body points (z=0) into camera space under the test pose."""
    rotation, tvec = _pose()
    body = np.zeros((len(body_xy), 3))
    body[:, :2] = body_xy
    return (rotation @ body.T + tvec.reshape(3, 1)).T


def _project(point_camera: np.ndarray) -> tuple[float, float]:
    p = CAMERA_MATRIX @ point_camera
    return float(p[0] / p[2]), float(p[1] / p[2])


def _slate_body() -> np.ndarray:
    """Six scattered points, the shape a hand-labelled dive slate produces."""
    return np.array(
        [
            [0.0, 0.0],
            [0.203, 0.0],
            [0.0, 0.254],
            [0.203, 0.254],
            [0.101, 0.0],
            [0.101, 0.254],
        ]
    )


def _checkerboard_body(cols: int = 14, rows: int = 10, square: float = 0.025):
    """Interior corners of a `cols` x `rows` grid — what detection returns.

    Deliberately a different origin, count and spacing from `_slate_body`: the
    point of the seam is that none of that reaches the answer.
    """
    xs, ys = np.meshgrid(np.arange(cols) * square, np.arange(rows) * square)
    return np.column_stack([xs.ravel(), ys.ravel()])


def _observed(body_xy: np.ndarray) -> np.ndarray:
    return np.array([_project(p) for p in _to_camera(body_xy)])


# ── the plane ─────────────────────────────────────────────────────────


def test_recovers_the_plane_a_known_pose_put_the_target_on():
    body = _slate_body()
    plane = plane_from_correspondences(body, _observed(body), CAMERA_MATRIX)

    assert plane is not None
    rotation, _ = _pose()
    # `rotation[:, 2]` is the plane normal: the body's +Z axis in camera space.
    assert np.allclose(plane.normal, rotation[:, 2], atol=1e-6)
    # Every correspondence must satisfy the plane equation it produced.
    for point in _to_camera(body):
        assert abs(float(plane.normal @ (point - plane.point))) < 1e-7


def test_solvepnp_does_not_refuse_a_degenerate_correspondence_set():
    """**A trap for the checkerboard producer, pinned here because it is not
    obvious.** Six coincident body points against six coincident pixels
    describe no pose at all, and `solvePnP` still returns success — so
    `plane_from_correspondences` hands back a confident, meaningless plane.

    The None path exists (it is `solvePnP`'s own `ret=False`) but it is NOT a
    degeneracy check, and nothing here can be made into one without changing
    stage-13 behaviour. A producer that generates correspondences
    automatically must therefore gate on its own detection quality — a
    partially-detected or collinear board has to be rejected before it gets
    this far, because it will not be rejected after.
    """
    body = np.zeros((6, 2))
    image = np.full((6, 2), 2048.0)

    plane = plane_from_correspondences(body, image, CAMERA_MATRIX)

    assert plane is not None  # documents the gap, does not endorse it


# ── the dot on the plane ──────────────────────────────────────────────


def test_recovers_the_three_d_position_of_a_dot_on_the_plane():
    body = _slate_body()
    plane = plane_from_correspondences(body, _observed(body), CAMERA_MATRIX)

    # A point on the target that is NOT one of the correspondences.
    truth = _to_camera(np.array([[0.077, 0.161]]))[0]
    recovered = laser_point_on_plane(plane, _project(truth), CAMERA_MATRIX)

    assert recovered is not None
    assert np.allclose(recovered, truth, atol=1e-6)


def test_a_ray_parallel_to_the_plane_yields_no_intersection():
    """The one real failure mode of the intersection itself. A ray that never
    meets the plane has no 3-D position, and returning None keeps it out of the
    fit instead of contributing a point at infinity."""
    from fishsense_core.world_point import WorldPointHandler

    pixel = (2600.0, 1800.0)
    ray = (
        WorldPointHandler(np.linalg.inv(CAMERA_MATRIX)).project_image_point(
            np.array(pixel, dtype=np.float64)
        )
        * -1
    )
    # A plane whose normal is perpendicular to that exact ray.
    normal = np.cross(ray, np.array([0.0, 1.0, 0.0]))
    normal = normal / np.linalg.norm(normal)
    assert abs(float(normal @ ray)) < 1e-12

    parallel = Plane(normal=normal, point=np.array([0.0, 0.0, 1.5]))

    assert laser_point_on_plane(parallel, pixel, CAMERA_MATRIX) is None


# ── the property the checkerboard plan rests on ───────────────────────


def test_the_target_shape_does_not_reach_the_answer():
    """**The seam.** A slate's six hand-clicked points and a checkerboard's 140
    detected corners, describing the same physical plane, must recover the same
    dot position.

    This is the whole basis for adding checkerboard calibration as a second
    producer of `LaserExtrinsics` rather than a second pipeline: the target
    differs only in how `(body_points, image_points)` are obtained, and nothing
    downstream — the ray-plane intersection, `calibrate_laser`, the
    self-consistency gate — can tell which one it was.
    """
    slate_body = _slate_body()
    board_body = _checkerboard_body()

    slate_plane = plane_from_correspondences(
        slate_body, _observed(slate_body), CAMERA_MATRIX
    )
    board_plane = plane_from_correspondences(
        board_body, _observed(board_body), CAMERA_MATRIX
    )
    assert slate_plane is not None and board_plane is not None

    # Same physical plane, expressed from two different body origins.
    assert np.allclose(slate_plane.normal, board_plane.normal, atol=1e-6)

    truth = _to_camera(np.array([[0.088, 0.133]]))[0]
    pixel = _project(truth)

    from_slate = laser_point_on_plane(slate_plane, pixel, CAMERA_MATRIX)
    from_board = laser_point_on_plane(board_plane, pixel, CAMERA_MATRIX)

    assert np.allclose(from_slate, truth, atol=1e-6)
    assert np.allclose(from_board, truth, atol=1e-6)
    assert np.allclose(from_slate, from_board, atol=1e-9)
