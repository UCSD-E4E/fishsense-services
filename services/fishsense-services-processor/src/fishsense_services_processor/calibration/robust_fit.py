"""Drop observations that disagree with the laser ray before fitting it.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/src/fishsense_data_processing_workflow_worker/robust_laser_fit.py.

`fishsense_core.laser.calibrate_laser` is plain least squares with no outlier
rejection, so one badly-placed observation drags the whole line. Prod dive 77
is the worked example: labelers clicked the laser's specular reflection on
about half its frames and the persisted calibration produced +137% length
errors. Dive 103 is the quieter version — a 6.25 cm baseline from 17 otherwise
clean observations, which neither a higher `MIN_LASER_POINTS` nor the baseline
bound explains.

**The geometry amplifies it.** The fit reports where the ray crosses z=0,
typically a metre or more behind the observations, so a small angular error
from one bad point is levered into a large error in the *baseline* — the one
quantity `check_fit_self_consistency` provably cannot see, because sliding the
offset leaves the ray's projection unchanged.

Trimming is deliberately conservative. It is a guard against a few bad
observations, not a way to rescue a badly contaminated dive: past
`MAX_TRIM_FRACTION` it declines to trim at all and lets the baseline gate
refuse the result, because when most observations disagree there is no majority
worth trusting and quietly fitting the larger half would produce a confident
answer from data we have no reason to believe.

Pure numpy, no Temporal and no `fishsense_core`, so it is unit-testable
anywhere and can be reasoned about on synthetic geometry with a known answer.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "MAD_FLOOR_M",
    "MAX_TRIM_FRACTION",
    "MIN_POINTS_TO_TRIM",
    "OUTLIER_SIGMA",
    "trim_outlying_observations",
]

#: Residual, in multiples of the scaled MAD, past which an observation is
#: dropped. Matches `DEFAULT_OUTLIER_SIGMA` in the laser-label line fit, which
#: solves the same shape of problem in 2-D and was tuned on real dives.
OUTLIER_SIGMA = 3.0

#: Floor on the MAD, in metres. Without it a tight set collapses the scale to
#: near zero and ordinary sub-millimetre scatter reads as a 3-sigma outlier —
#: the same reason `LABEL_NOISE_MAD_FLOOR_PX` exists on the 2-D fit.
MAD_FLOOR_M = 0.003

#: Below this many observations, trimming is skipped entirely. Two points fit a
#: line exactly, so every residual is zero and any rule is reading noise; a few
#: more still leave no redundancy worth spending. Returning them untouched
#: keeps a thin-but-valid calibration from becoming no calibration.
MIN_POINTS_TO_TRIM = 6

#: Never drop more than this fraction. A set where most observations disagree
#: has no majority to trust — see the module docstring.
MAX_TRIM_FRACTION = 0.4


def _fit_line(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Total-least-squares 3-D line -> (centroid, unit direction).

    Deliberately not `calibrate_laser`: this only needs the line the points
    lie on, not the z=0 crossing the Rust kernel reports, and keeping the
    trimmer free of that dependency is what lets it be tested on synthetic
    geometry without the kernel in the loop.
    """
    centroid = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - centroid)
    return centroid, vt[0]


def _residuals(points: np.ndarray) -> np.ndarray:
    """Perpendicular distance from each point to the best-fit line."""
    centroid, direction = _fit_line(points)
    offsets = points - centroid
    along = (offsets @ direction)[:, None] * direction[None, :]
    return np.linalg.norm(offsets - along, axis=1)


def trim_outlying_observations(
    points,
    *,
    sigma: float = OUTLIER_SIGMA,
    mad_floor_m: float = MAD_FLOOR_M,
    max_trim_fraction: float = MAX_TRIM_FRACTION,
) -> np.ndarray:
    """Return `points` without the observations that disagree with the ray.

    One pass, not iterated to convergence. A single robust pass removes the
    gross disagreements that actually move the baseline, while repeated
    re-fitting on a shrinking set walks toward whichever subset happens to be
    most collinear — which on a nearly-straight ray is close to arbitrary.

    Never mutates the input.
    """
    observations = np.asarray(points, dtype=float)
    if observations.ndim != 2 or len(observations) < MIN_POINTS_TO_TRIM:
        return observations.copy()

    residuals = _residuals(observations)
    median = float(np.median(residuals))
    # Scaled MAD: 1.4826 makes it a consistent estimator of the standard
    # deviation for Gaussian noise, so `sigma` means what it says.
    mad = 1.4826 * float(np.median(np.abs(residuals - median)))
    scale = max(mad, mad_floor_m)

    keep = residuals <= median + sigma * scale
    dropped = int(len(observations) - keep.sum())
    if dropped > int(len(observations) * max_trim_fraction):
        # No trustworthy majority — keep everything and let the baseline gate
        # judge the result, rather than confidently fitting the larger half.
        return observations.copy()
    return observations[keep]
