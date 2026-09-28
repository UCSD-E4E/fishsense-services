"""What a physically plausible laser baseline is, spelled once.

Ported verbatim from fishsense-lite@77e8f8e5
libs/fishsense-shared/src/fishsense_shared/calibration_bounds.py. v2 change:
it lives in the contracts package, next to `taxonomy`, for v1's reason --
two services need the identical number and neither owns it:

* the **processor** refuses to return a fit outside these bounds as accepted
  (`calibration.consistency.check_baseline_plausible`);
* the **API** excludes an already-stored calibration outside them from
  counting as a calibration at all (migration 0018's SQL function
  ``plausible_laser_baseline``, whose two numbers the API's tests pin to
  these), so its dive re-enters the calibration cohorts instead of being
  measured against a fit we know is wrong.

The baseline is `norm(laser_position[:2])` — the offset between the camera
centre and the laser on one rig. It is hardware, not a property of a dive, and
measurement says so loudly: over all 35 stored calibrations its interquartile
range is **9.99-10.45 cm**, half a centimetre across both producers, every
camera and two years.

A copy in each would be the drift this repo keeps rediscovering, and here the
two halves disagreeing is especially quiet: the api would hand a dive back for
recalibration that the processor then persists unchanged, hourly, forever.

**Why bounding the answer is the only check that works.** Nothing upstream
distinguishes a good fit from a bad one. Measured 2026-09-11 by rendering the
detected lattices: 60 of 61 frames found the full 14x10 board on good and bad
dives alike. Observation count does not separate them (one dive fits 2.60 cm
from 19 clean observations, three fit correctly from 2), nor does depth spread,
nor detection rate. And `check_fit_self_consistency` is structurally blind to
the baseline: it compares the fitted ray's *projection* to its 2-D dots, and
sliding the laser's offset leaves that projection identical.
"""

from __future__ import annotations

import math

__all__ = [
    "MAX_BASELINE_M",
    "MIN_BASELINE_M",
    "MIN_LASER_POINTS",
    "baseline_m",
    "is_plausible_baseline",
]

#: Minimum usable laser observations before a fit is attempted.
#:
#: From fishsense-lite@77e8f8e5 perform_laser_calibration_activity.py, where
#: v1 noted that the api's `MIN_SLATE_LASER_POINTS` must equal it: they are one
#: threshold spelled twice, on opposite sides of the worker boundary, and a
#: dive that clears the cohort's copy but not this one is re-selected hourly
#: forever with nothing written. Not hypothetical: the cohort used to count
#: completed slate labels rather than observations, and prod dive 347 (18
#: labels, 1 live dot) wedged stage 13 for as long as it was scheduled. v2
#: keeps it here, beside the baseline, for the same reason; the API's cohorts
#: spell it in SQL and a test pins the two.
MIN_LASER_POINTS = 2

#: Bounds on the baseline, in metres.
#:
#: **Placed midway between the populations, not hard against the healthy one.**
#: The floor moved from 7.8 to 9.7 cm on 2026-09-12. Two fits inside the old
#: bound, 8.90 cm (dive 502, borrowed by 503/504) and 9.51 cm (dive 498), had
#: been called healthy on the strength of a ~-1 % median length error. The
#: range trend of a rigid target showed why the median lied: -14 to -18 % at
#: 0.8 m rising to ~0 at 4 m -- the short baseline's flat scale error and a
#: compensating angle error cancel exactly where the median and p90 sit. Sound
#: calibrations (10.3-10.5 cm) are flat across range to within 1 %. So the
#: nearest bad fit below is now 9.51 and the smallest sound one 9.87 (dive 94);
#: 9.7 sits between them. Above, healthy 12.95 and bad 16.01 are unchanged.
#:
#: **Widen only against re-measured data**, and measure with the range trend,
#: not the median: a wrong baseline is the one error the rest of the pipeline
#: provably cannot see. It scales every depth, hence every length, while
#: reprojection residual and self-consistency stay clean -- and, as above, a
#: compensating angle error can hide it from a known-length median too.
MIN_BASELINE_M = 0.097
MAX_BASELINE_M = 0.145


def baseline_m(laser_position) -> float:
    """The in-plane offset of `laser_position`, in metres.

    Only x and y are read. Both producers return the fit's origin as the point
    where the laser ray crosses the camera's z=0 plane and pad z to zero, so a
    three-component norm would depend on padding that carries no information.

    Returns `inf` for anything unreadable — a short vector, a non-numeric
    entry — so callers get the same answer they would for an absurd baseline
    rather than an exception. "Cannot tell" and "implausible" want the same
    treatment here: refuse, and keep looking.
    """
    try:
        x = float(laser_position[0])
        y = float(laser_position[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return float("inf")
    magnitude = (x * x + y * y) ** 0.5
    # NaN fails every comparison, so a caller's range test would ACCEPT it.
    # Map it onto the same answer as unreadable input.
    if math.isnan(magnitude):
        return float("inf")
    return magnitude


def is_plausible_baseline(
    laser_position,
    *,
    min_baseline_m: float = MIN_BASELINE_M,
    max_baseline_m: float = MAX_BASELINE_M,
) -> bool:
    """Whether `laser_position` describes a baseline a real rig could have."""
    return min_baseline_m <= baseline_m(laser_position) <= max_baseline_m
