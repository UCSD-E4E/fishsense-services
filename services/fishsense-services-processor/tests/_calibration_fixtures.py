"""Dive-dot stand-ins shared by the calibration-fit suites.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/_calibration_fixtures.py. v2 change: the dots are plain
pixels in the payload (`dive_dots`), not SDK laser labels behind a mocked
client -- the processor reads nothing, so there is no client to fake.

`check_calibration_describes_dive` compares a fit against **every live laser
dot in the dive**, not against the frames it was fitted from, so every test
that drives a fit end to end has to supply those dots. The stand-in is dots
lying exactly on the projection of the ray the fake kernel returns, i.e. "the
whole dive agrees".
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def dots_on_projected_ray(
    origin: Sequence[float],
    axis: Sequence[float],
    camera_matrix: Sequence[Sequence[float]],
    depths: Sequence[float] | None = None,
) -> np.ndarray:
    """The pixels a dot on `origin` + t * `axis` would occupy, per range."""
    origin = np.asarray(origin, dtype=float)
    axis = np.asarray(axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    depths = np.linspace(0.6, 3.5, 40) if depths is None else np.asarray(depths)
    ts = (depths - origin[2]) / axis[2]
    points = origin[None, :] + ts[:, None] * axis[None, :]
    homogeneous = (np.asarray(camera_matrix, dtype=float) @ points.T).T
    return homogeneous[:, :2] / homogeneous[:, 2:3]


def dive_dots_on_ray(
    origin: Sequence[float],
    axis: Sequence[float],
    camera_matrix: Sequence[Sequence[float]],
    depths: Sequence[float] | None = None,
    offset_px: float = 0.0,
) -> list[tuple[float, float]]:
    """The dive's dots, for the payload's `dive_dots`.

    `offset_px` slides the whole population perpendicular to its own long
    axis, which is how a dive that disagrees with its calibration looks: the
    dots are still collinear, just not on the fitted ray.
    """
    dots = dots_on_projected_ray(origin, axis, camera_matrix, depths)
    if offset_px:
        centred = dots - dots.mean(axis=0)
        _, _, vt = np.linalg.svd(centred)
        normal = np.array([-vt[0][1], vt[0][0]])
        dots = dots + offset_px * normal
    return [(float(x), float(y)) for x, y in dots]
