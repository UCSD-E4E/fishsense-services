"""Where a laser dot sits in space, given a planar target of known geometry.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/src/fishsense_data_processing_workflow_worker/calibration_geometry.py.

Stage 13 fits laser extrinsics from observations of the dot lying on a target
whose geometry is known. The target's *only* contribution is a **plane**: once
you have a normal and a point on it, the dot's 3-D position is the intersection
of its back-projected ray with that plane, and nothing about how the plane was
obtained survives into the answer.

That is worth stating as code rather than leaving inline, because a second
producer is coming. A large part of the pool-test corpus was shot against a
checkerboard rather than a dive slate
(`docs/plans/checkerboard-laser-calibration.md`), and a checkerboard differs
only in how `(body_points, image_points)` are obtained — detected corners at a
known square pitch instead of hand-clicked template points at a known DPI.
Everything from here down is shared:

    plane_from_correspondences  ->  laser_point_on_plane
                                ->  fishsense_core.laser.calibrate_laser
                                ->  check_fit_self_consistency
                                ->  LaserExtrinsics

The alternative — transcribing the ray-plane intersection a second time for the
checkerboard path — is precisely the drift `laser_geometry` was extracted to
prevent, and `duplicate-code` would not catch it: swapping `DiveSlateLabel` for
a checkerboard DTO is a systematic rename, which is textually invisible.

**`solvePnP` is given zero distortion on purpose.** The pipeline only ever feeds
it rectified imagery (`RectifiedImage` applies `cv2.undistort`), so the
correspondences are already in an undistorted frame. Any future caller must
detect its target on the *rectified* image for the same reason; passing raw
pixels here yields a plausible, slightly wrong pose and no error.

`test_calibration_geometry.py` pins the seam, including the property the
checkerboard plan rests on: a slate's six hand-clicked points and a
checkerboard's grid of detected corners, describing the same physical plane,
recover the same dot.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from fishsense_core.world_point import WorldPointHandler

__all__ = ["Plane", "laser_point_on_plane", "plane_from_correspondences"]


@dataclass(frozen=True)
class Plane:
    """A plane in camera space: unit `normal`, and any `point` lying on it."""

    normal: np.ndarray
    point: np.ndarray


def plane_from_correspondences(
    body_points,
    image_points,
    camera_matrix: np.ndarray,
) -> Plane | None:
    """Recover the plane a planar target occupied, from 2-D <-> 3-D pairs.

    `body_points` are the target's own coordinates in **metres**, `(N, 2)` or
    `(N, 3)` with z=0; `image_points` are where those landed in the rectified
    image, `(N, 2)`, in the same order. Ordering is the caller's contract:
    `solvePnP` pairs them purely by position.

    Returns None when no pose is recoverable, so one unusable observation skips
    rather than failing the whole dive.
    """
    body = np.zeros((len(body_points), 3), dtype=np.float32)
    body[:, :2] = np.asarray(body_points, dtype=np.float64)[:, :2]
    image = np.asarray(image_points, dtype=np.float64)

    ret, rvec, tvec = cv2.solvePnP(
        body,
        image,
        camera_matrix,
        np.zeros((5,)),
    )
    if not ret:
        return None

    rotation, _ = cv2.Rodrigues(rvec)
    camera_space_points = (rotation @ body.T + tvec).T
    # The body's +Z axis in camera space. The target is z=0 in its own frame,
    # so this is the plane normal — and it is all the in-plane pose that
    # matters here: nothing downstream reads the rotation about it, which is
    # why a checkerboard's 180-degree ambiguity and origin-corner choice are
    # irrelevant to this use.
    return Plane(normal=rotation[:, 2], point=camera_space_points[0, :])


def laser_point_on_plane(
    plane: Plane,
    laser_image_point,
    camera_matrix: np.ndarray,
) -> np.ndarray | None:
    """Intersect the dot's back-projected ray with `plane`.

    Returns None when the ray is unusable (NaN) or runs parallel to the plane,
    so the observation is dropped instead of contributing a point at infinity.
    """
    k_inv = np.linalg.inv(camera_matrix)
    ray = (
        WorldPointHandler(k_inv).project_image_point(
            np.asarray(laser_image_point, dtype=np.float64)
        )
        * -1
    )
    if np.any(np.isnan(ray)):
        return None

    denominator = float(plane.normal.T @ ray)
    if denominator == 0.0:
        return None

    scale = (plane.normal.T @ plane.point) / denominator
    point = ray * scale
    if np.any(np.isnan(point)):
        return None
    return point
