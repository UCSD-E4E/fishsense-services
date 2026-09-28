"""Laser projection: where the dot is, and how long the fish is at that depth.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/laser_geometry.py.
The kernel and its rules are v1's; the one v2 change is the signature: the
processor is handed the camera matrix in the processing contract, not v1's SDK
`CameraIntrinsics`, so the functions take the matrix itself.

Two stages need the same projection. Stage 14 (`measurement`) turns a laser
dot into a depth and measures head-to-tail across a plane at that depth; the
depth stage (`laser_depth`) records the depth itself, for every frame with a
validated laser rather than only the measurable ones. A second transcription
of it is precisely the kind of copy that drifts -- the sign convention alone
cost a synthetic-geometry investigation
(`test_stage14_pipeline_sign_consistency.py`,
`test_compute_world_point_from_depth_convention.py`).

The kernel is `fishsense_core.world_point.WorldPointHandler`.

The triangulation has no failure mode, and that is the thing to understand
about it. It returns the least-squares closest point between the camera ray
and the laser ray, which always exists -- so for a pixel that no real laser dot
could have produced it answers just as confidently, with a point at or behind
the camera. A laser ray parallel to the image plane through the camera centre
gives the origin; a dot on the wrong side of the principal point for the
laser's offset gives a negative Z. Both are the *correct* answer to the
question that was asked; neither corresponds to an observation, and a length
computed at a negative depth is numerically identical to one at its positive
twin (the back-projection is linear in depth, so head and tail move together).
Callers must gate on `depth_m > 0`.

The solve also returns its closest-approach `residual` -- how well the dot and
the calibration actually agree. Recorded per image, but not a substitute for
the depth gate: see `LaserPoint` for what it cannot see. `test_laser_geometry.py`
pins all of this.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from fishsense_core.world_point import WorldPointHandler

__all__ = ["LaserPoint", "compute_laser_point", "measure_length_at_depth"]


@dataclass(frozen=True)
class LaserPoint:
    """Where the laser dot was, in metres, in camera coordinates.

    `depth_m` is the Z component and `range_m` the Euclidean norm. They differ
    by the dot's off-axis angle and are equal only on the optical axis, so
    they are carried separately rather than left for a caller to conflate:
    stage 14's back-projection needs the depth specifically (it measures
    across a fronto-parallel plane), while "how far away was it" means the
    range.

    `residual_m` is how close the camera ray and the laser ray actually came
    to meeting -- ~0 when the dot is genuinely consistent with the calibration,
    which makes it a direct per-dot check of a laser label against its
    extrinsics. Necessary, not sufficient, and the ways it falls short are
    specific: it is blind to error *along* the laser's epipolar line (a dot
    slid along it moves the depth a long way with the residual pinned at the
    noise floor), two rays meeting at the camera centre report 0 at zero or
    negative depth, it is metric so one threshold is stricter close up than
    far away, and the float32 solve puts its noise floor near 1e-5 m at metre
    scale. Pair it with the depth gate; never replace the depth gate with it.
    """

    depth_m: float
    range_m: float
    residual_m: float


def _handler(camera_matrix) -> WorldPointHandler:
    return WorldPointHandler(np.linalg.inv(np.asarray(camera_matrix, dtype=float)))


def compute_laser_point(laser_label, laser_calibration, camera_matrix) -> LaserPoint:
    """Triangulate the laser dot from its pixel and the rig's calibration.

    `laser_label` carries `x`, `y`; `laser_calibration` carries
    `laser_position` and `laser_axis`. The dot's pixel fixes a ray out of the
    camera; the calibration fixes the laser's ray in space; the dot is the
    least-squares closest point between the two, and `residual_m` is how far
    apart they still were there.

    The axis magnitude does not matter -- fishsense-core normalises it -- and
    a zero-length axis raises `ValueError` rather than coming back as a
    plausible-looking point. The raise is deliberately not caught here: the
    axis belongs to the dive's calibration, so a directionless one makes
    every image in that dive unprocessable, and failing the activity is the
    honest blast radius.
    """
    laser3d, residual = _handler(
        camera_matrix
    ).compute_world_point_from_laser_with_residual(
        np.asarray(laser_calibration.laser_position, dtype=float),
        np.asarray(laser_calibration.laser_axis, dtype=float),
        np.array([laser_label.x, laser_label.y]),
    )
    return LaserPoint(
        depth_m=float(laser3d[2]),
        range_m=float(np.linalg.norm(laser3d)),
        residual_m=float(residual),
    )


def measure_length_at_depth(headtail_label, depth_m: float, camera_matrix) -> float:
    """Head-to-tail distance in metres, with both keypoints placed at
    `depth_m`.

    Single-depth by construction: the laser gives one range per frame, so the
    fish is measured as its *projection* onto a plane at that depth. An
    out-of-plane fish therefore reads short, never long -- the foreshortening
    the accuracy analysis attributes its one-sided negative tail to.
    """
    handler = _handler(camera_matrix)
    head3d = handler.compute_world_point_from_depth(
        np.array([headtail_label.head_x, headtail_label.head_y]), depth_m
    )
    tail3d = handler.compute_world_point_from_depth(
        np.array([headtail_label.tail_x, headtail_label.tail_y]), depth_m
    )
    return float(np.linalg.norm(head3d - tail3d))
