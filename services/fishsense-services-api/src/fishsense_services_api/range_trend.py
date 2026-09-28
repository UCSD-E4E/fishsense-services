"""Scale-free range-trend audit of a dive's laser calibration.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/range_trend.py,
with its tests (tests/test_range_trend.py). The estimator, thresholds and
notes are v1's, unchanged; the findings below are v1's, measured on v1's
corpus. v2 changes: `group_by_object` keys a measurement by its `capture_id`
(v1: `image_id`) and names a wild fish by its `fish_number` (v1: `fish_id`).
The inputs are read by `range_trend_store`; `fishsense-services-api
audit-range-trend` is v1's scripts/audit_length_range_trend.py.

WHAT IT SEES THAT NOTHING ELSE DOES. The image of the laser line fixes only
the plane through the camera centre that contains it. Where the laser sits
*within* that plane -- the in-plane angle that sets metric scale -- moves the
dots by ~1e-13 px, so every check that looks at the dots is blind to it:
reprojection residual (measured rho = -0.03 against length error), the
7.8-14.5 cm baseline plausibility gate (three baseline outliers all measure
within 1.6 %), and the dot-on-board depth check (the calibration was fitted
from those frames). An in-plane error eps does, however, leave a signature in
the *measurements*: depth goes as b*f/d, a rotation shifts the vanishing point
by f*eps, and to first order

    z_measured = z * (1 + eps * z / b)

so a rigid object reads a length that changes linearly with range, at
eps / b per metre. A 0.15 deg error on a 10.3 cm baseline is -2.5 % per metre.

WHY IT IS SCALE-FREE. "The same object must read the same length at every
range" spends no reference length. The slope is fitted on the object's own
measurements; a known length is never consulted, so the test cannot be
circular in the way anchoring a calibration to a fish model is. That is the
HANDOFF's ruler-at-two-ranges argument, applied to every rigid object a dive
carries.

WHAT IT MEASURED ON THE CORPUS (2026-09-12, 2,927 measurements, 32 dives; see
imwut_2026_fishsense_lite/post_labeling_analysis/HANDOFF.md). Fitted on frames
at >= 0.8 m with >= 8 frames spanning a >= 2x range:

  * Negative slopes: every cell whose interval sat below -2 %/m was a
    calibration already known bad from the known lengths -- dive 490 at
    -13.3 %/m (recovered angle -0.78 deg against 0.82 deg measured directly
    between its two calibration bursts), its borrowers 491 / 492 at
    -3.8 / -2.8 (-0.22 / -0.17 deg against 0.21 / 0.25 from the known
    lengths), 494 at -6.1 and 509 at -4.1. The two August repairs, 60 and 76,
    read -2.3 on each target (-0.14 deg against the 0.146 deg the repair
    fitted) with intervals reaching -1.1, so they are seen but not flagged.
  * Positive slopes are calibration signal too, and were at first misread.
    They track SHORT fitted baselines: 503 / 504 (borrowing dive 502 at
    8.90 cm) read +5.3 / +5.5 %/m, 498 (9.51 cm) +2.8, 501 (10.12) +1.6,
    while every 10.3-10.5 cm calibration sits within -1.0..+0.9. The per-bin
    structure says what it is: -14 to -18 % at 0.8 m rising to ~0 at 4 m, a
    short baseline's flat scale error paired with a compensating angle error
    that cancel exactly where the median and p90 sit -- which is how those
    dives graded "-1 %" against the known lengths and passed both the cohort
    rule and the old 7.8 cm baseline floor. So both signs flag. What remains
    ambiguous on the positive side is object-specific: the single-object
    angle sessions (87 / 114 are positive even broadside-only) and the Shark
    on dive 76 (+5.3 while the Purple Angel on the same dive reads -2.2, which
    one angle cannot do). A flag on a normal multi-frame rigid target is a
    calibration finding; a flag on those two kinds is not, and the note says
    which reading applies to the sign.
  * A milder close-range under-read remains on the Weasly Fish alone (near
    -6/-7 % against far -2/-4 % on sound dives; the Box shows none). It is
    concentrated below 0.8 m, which is why those frames are dropped before
    the fit rather than after.
  * It is blind to a range-flat scale error on its own. Dives 506 and 507
    both borrow dive 505 and over-read (+4.3 / +2.2 pp) with slopes of +0.8
    and +0.1 %/m. A baseline error that the fit did NOT pair with an angle
    error stays invisible here; the baseline plausibility gate is the check
    for that.

WHERE IT CAN RUN. It needs one rigid object measured many times across a wide
range spread. Every rigid-target pool dive has that (33 usable cells over 25
dives). No wild-fish dive does: the five fish in prod with >= 5 measurements
span at most a 1.5x range. So this is a post-hoc audit for the validation
corpus, not a stage-13 gate (stage 13 has only the board frames the fit came
from) and not, today, a field check -- it would become one if the protocol
had the diver photograph a rigid reference at two ranges after calibrating.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable, Mapping

import numpy as np

#: Frames closer than this are dropped before the fit. The corpus-wide
#: close-range under-read lives below it and would read as a positive trend.
DEFAULT_MIN_DEPTH_M = 0.8
#: Fewer frames than this, or a narrower range spread than this ratio, and
#: the slope's interval is too wide to say anything -- the audit returns None
#: rather than a confident number over a handful.
DEFAULT_MIN_FRAMES = 8
DEFAULT_MIN_RANGE_RATIO = 2.0
#: A cell flags when its whole confidence interval sits beyond +-this. On the
#: corpus every sound-baseline cell sat within -1.0..+0.9 %/m and the known-bad
#: ones beyond -2.8 or +2.8; the threshold sits between.
FLAG_SLOPE_PCT_PER_M = 2.0

_CI_Z = 1.96  # 95 % two-sided


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Theil-Sen slope of y on x with Sen's 95 % confidence interval.

    The median of all pairwise slopes, so one wild frame -- a mislabelled
    head or tail -- cannot pull it. Returns (slope, ci_low, ci_high); on an
    exact line all three coincide. Fewer than three points is refused: two
    points have one slope and no interval.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = x.size
    if n < 3 or y.size != n:
        raise ValueError("theil_sen needs at least three (x, y) pairs")
    i, j = np.triu_indices(n, k=1)
    dx = x[j] - x[i]
    keep = dx != 0
    slopes = np.sort((y[j] - y[i])[keep] / dx[keep])
    if slopes.size == 0:
        raise ValueError("theil_sen needs at least two distinct x values")
    slope = float(np.median(slopes))
    # Sen (1968), as scipy.stats.theilslopes does it without the tie term.
    sigma = np.sqrt(n * (n - 1) * (2 * n + 5) / 18.0)
    c = _CI_Z * sigma
    m = slopes.size
    lo_idx = min(max(int(round((m - c) / 2.0)), 0), m - 1)
    hi_idx = min(max(int(round((m + c) / 2.0)) - 1, 0), m - 1)
    return slope, float(slopes[lo_idx]), float(slopes[hi_idx])


@dataclass(frozen=True)
class RangeTrend:
    """One rigid object's length-against-range trend on one dive."""

    n: int
    depth_range_m: tuple[float, float]
    slope_pct_per_m: float
    ci_pct_per_m: tuple[float, float]  # Sen's 95 % interval on the slope
    eps_deg: float
    flagged: bool
    note: str


def range_trend(
    depths_m: Iterable[float],
    lengths_m: Iterable[float],
    baseline_m: float,
    *,
    min_frames: int = DEFAULT_MIN_FRAMES,
    min_range_ratio: float = DEFAULT_MIN_RANGE_RATIO,
    min_depth_m: float = DEFAULT_MIN_DEPTH_M,
    flag_slope_pct_per_m: float = FLAG_SLOPE_PCT_PER_M,
) -> RangeTrend | None:
    """Fit measured length against laser depth for one rigid object.

    Length is fitted linearly in depth and the slope divided by the fitted
    length at zero range, so under `z (1 + eps z / b)` the relative slope is
    exactly eps / b and `eps_deg` is exact. No known length is used.

    Returns None when the data cannot support a slope (too few frames beyond
    `min_depth_m`, or too narrow a range spread). Raises on a non-positive
    depth or length: those are upstream defects, not data to fit around.
    """
    z = np.asarray(list(depths_m), dtype=float)
    length = np.asarray(list(lengths_m), dtype=float)
    if z.size != length.size:
        raise ValueError("depths and lengths differ in length")
    if np.any(z <= 0) or np.any(length <= 0):
        raise ValueError("depths and lengths must be positive")
    keep = z >= min_depth_m
    z, length = z[keep], length[keep]
    if z.size < min_frames:
        return None
    if z.max() / z.min() < min_range_ratio:
        return None

    slope, lo, hi = theil_sen(z, length)
    intercept = float(np.median(length - slope * z))
    if intercept <= 0:
        return None
    slope_pct, lo_pct, hi_pct = (100.0 * v / intercept for v in (slope, lo, hi))
    eps_deg = float(np.degrees(slope / intercept * baseline_m))
    flagged = bool(hi_pct < -flag_slope_pct_per_m or lo_pct > flag_slope_pct_per_m)
    return RangeTrend(
        n=int(z.size),
        depth_range_m=(float(z.min()), float(z.max())),
        slope_pct_per_m=slope_pct,
        ci_pct_per_m=(lo_pct, hi_pct),
        eps_deg=eps_deg,
        flagged=flagged,
        note=_note(flagged, slope_pct, eps_deg, flag_slope_pct_per_m),
    )


def _note(flagged: bool, slope_pct: float, eps_deg: float, threshold: float) -> str:
    if flagged and slope_pct < 0:
        return (
            f"length falls with range: in-plane calibration error ~{eps_deg:+.2f} deg "
            "(rotated axis)"
        )
    if flagged:
        return (
            f"length rises with range: in-plane calibration error ~{eps_deg:+.2f} deg; "
            "on the corpus this sign meant a short fitted baseline paired with a "
            "compensating angle -- check the baseline. Not a calibration finding "
            "on a single-object oblique session or the Shark."
        )
    if abs(slope_pct) > threshold:
        return "trend beyond threshold but the interval does not clear it; not flagged"
    return ""


def group_by_object(
    measurements: Iterable,
    depth_by_capture: Mapping[Hashable, float],
    name_by_capture: Mapping[Hashable, str | None],
) -> dict[str, tuple[list[float], list[float]]]:
    """Pair each measurement with its capture's laser depth, keyed by object.

    A rigid target is keyed by its taxonomy name (from `name_by_capture`, e.g.
    "Weasly Fish", "Box"); a measurement whose capture has no rigid-target
    name is keyed by its `fish_number` as "fish <number>" -- one wild fish is
    one rigid object too, on the rare dive that measures it often enough.
    Measurements without a depth, a length, or an object are dropped.
    """
    groups: dict[str, tuple[list[float], list[float]]] = {}
    for m in measurements:
        depth = depth_by_capture.get(m.capture_id)
        if depth is None or m.length_m is None:
            continue
        name = name_by_capture.get(m.capture_id)
        if name is None:
            if m.fish_number is None:
                continue
            name = f"fish {m.fish_number}"
        zs, ls = groups.setdefault(name, ([], []))
        zs.append(float(depth))
        ls.append(float(m.length_m))
    return groups
