"""The laser point the depth stage, stage 14 and the automatic stage share.

`laser_point` is fishsense-core's `world_point.compute_laser_point`, with
its `ValueError` named for the calibration that is broken: core raises it
for a zero or non-finite laser axis (the dive's laser calibration) and for
a camera matrix that is not 3x3, not finite or singular (its camera
calibration). Each is the dive's, not an image's, so each fails the
activity non-retryably. Before (review of #361) all three stages reported
a broken camera matrix as an unusable laser calibration.

`dot_refusal` names why a point cannot carry a depth, the one vocabulary
the three stages store (`laser_depth.DotRefusal`). Both lived in the depth
stage's activity module, which the other two imported (review of #361).
"""

from __future__ import annotations

import importlib.metadata
import math

from fishsense_core.world_point import LaserPoint, compute_laser_point
from temporalio.exceptions import ApplicationError

__all__ = ["core_version", "dot_refusal", "laser_point"]


def core_version() -> str:
    """The fishsense-core wheel doing the geometry, as results record it."""
    return importlib.metadata.version("fishsense-core")


def laser_point(
    dot_xy, laser_position, laser_axis, camera_matrix, *, of: str
) -> LaserPoint:
    """Core's laser point for ``dot_xy``. A calibration it cannot use is a
    non-retryable `UnusableCameraCalibration` or `UnusableLaserCalibration`;
    ``of`` names whose (e.g. ``"laser calibration <id> of dive <id>"``)."""
    try:
        return compute_laser_point(dot_xy, laser_position, laser_axis, camera_matrix)
    except ValueError as exc:
        camera = str(exc).startswith("camera_matrix")
        raise ApplicationError(
            f"the {'camera' if camera else 'laser'} calibration behind {of} "
            f"is unusable: {exc}",
            type="UnusableCameraCalibration" if camera else "UnusableLaserCalibration",
            non_retryable=True,
        ) from exc


def dot_refusal(point: LaserPoint) -> str | None:
    """Why `point` cannot carry a depth (`laser_depth.DotRefusal`), or None.

    The gate is `point.is_measurable()`, whole; the name is its first failure:
    a depth that is not finite, then one at or behind the camera (so a dot
    v20 refused keeps its reason), then too little parallax. A dot with a
    finite, positive depth and enough parallax fails only if its range or
    residual is not finite -- the solve broke down, so it is `non_finite_depth`.
    """
    if point.is_measurable():
        return None
    if not math.isfinite(point.depth_m):
        return "non_finite_depth"
    if point.depth_m <= 0.0:
        return "non_positive_depth"
    if not all(
        map(math.isfinite, (point.range_m, point.residual_m, point.parallax_rad))
    ):
        return "non_finite_depth"
    return "low_parallax"
