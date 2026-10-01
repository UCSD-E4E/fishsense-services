"""The gate that asks whether a calibration describes the dive it will measure.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_calibration_describes_dive.py.

`check_fit_self_consistency` compares the fitted ray's projection against the
dots it was computed from, so it is satisfied by construction and says nothing
when those dots are few. On prod dive 347 it said nothing at all: the fit came
from ONE frame carrying a duplicate laser label at the identical pixel, so the
dot set had N=1 and the check abstained, while `MIN_LASER_POINTS = 2` was
satisfied by counting the duplicate as a second observation. The calibration
that resulted is 9.4 px away from the dive's own 321 laser dots.

This gate asks the other question: project the fitted ray and compare it
against **every live dot in the dive**, including the measurement frames the
calibration was never fitted to. That is not tautological — it is the check
that the laser was in the same state for the frames being measured as it was
for the frames it was calibrated from, which is precisely what a mid-dive
re-seat breaks (prod dive 490, where the board burst and the fish frames
disagreed by 0.82 deg).

Measured over all 32 stored calibrations against their dives' live dots
(2026-09-13), median perpendicular offset in px:

    0.43 - 0.88   twenty dives
    1.31 - 1.55   six dives
    2.01, 2.27    dives 279, 509
    3.33, 3.92    dives 341, 349   <- real burst-vs-frames disagreement
    5.37          dive 498         <- also caught by the baseline gate
    9.42          dive 347         <- the degenerate fit, caught by nothing else

The bounds sit between those populations, not against the healthy one — the
same rule the baseline gate uses, and for the same reason: a refusal wedges
the dive in its cohort, so a false positive is expensive.

**It is blind to the baseline, deliberately and unavoidably.** Sliding the
laser's offset along the family of rays that share an image line leaves the
projection identical, so five of the six known-bad baselines sit at 0.43-1.35
px here and pass. That is what `check_baseline_plausible` is for; the two
gates are complementary and neither subsumes the other.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.calibration.consistency import (
    DEFAULT_MAX_DIVE_MEDIAN_OFFSET_PX,
    DEFAULT_MAX_DIVE_P90_OFFSET_PX,
    MIN_DISAGREEING_DIVE_DOTS,
    CalibrationDoesNotDescribeDiveError,
    check_calibration_describes_dive,
)

# A realistic Olympus rig: the intrinsics and laser geometry of prod dive 347's
# camera, so the pixel scales in these tests are the ones the gate really sees.
K = np.array(
    [
        [2855.292793016781, 0.0, 2031.7894533541553],
        [0.0, 2881.0734769839373, 1447.6913944790074],
        [0.0, 0.0, 1.0],
    ]
)
ORIGIN = np.array([-0.0306, -0.0977, 0.0])
AXIS = np.array([0.00737, 0.03204, 0.99946])


def _dots_on_ray(origin=ORIGIN, axis=AXIS, depths=None) -> np.ndarray:
    """The pixels a dot would occupy at a spread of ranges — i.e. dots that lie
    exactly on the projection of `origin` + t * `axis`."""
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    depths = np.linspace(0.6, 3.5, 40) if depths is None else np.asarray(depths)
    ts = (depths - origin[2]) / axis[2]
    points = np.asarray(origin, float)[None, :] + ts[:, None] * axis[None, :]
    homogeneous = (K @ points.T).T
    return homogeneous[:, :2] / homogeneous[:, 2:3]


def _perpendicular(dots: np.ndarray, px: float) -> np.ndarray:
    """Slide `dots` `px` pixels perpendicular to their own long axis."""
    centred = dots - dots.mean(axis=0)
    _, _, vt = np.linalg.svd(centred)
    normal = np.array([-vt[0][1], vt[0][0]])
    return dots + px * normal


# --- the gate passes what it should -----------------------------------------


def test_dots_on_the_projected_ray_pass():
    check_calibration_describes_dive(ORIGIN, AXIS, K, _dots_on_ray())


def test_realistic_label_noise_passes():
    """A sound dive sits at 0.4-2.3 px; a couple of px of label noise is normal."""
    rng = np.random.default_rng(0)
    dots = _perpendicular(_dots_on_ray(), 0.0) + rng.normal(0, 1.5, (40, 2))
    check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


def test_the_gate_is_blind_to_the_baseline_by_construction():
    """Scaling the laser's offset moves the ray but not its projection, so this
    gate cannot see a wrong baseline. Pinned so nobody expects it to."""
    dots = _dots_on_ray()
    for scale in (0.5, 2.0, 4.0):
        check_calibration_describes_dive(ORIGIN * scale, AXIS, K, dots)


# --- and refuses what it should ---------------------------------------------


def test_a_uniformly_offset_dot_population_is_refused():
    """Dive 347's shape: the calibration's line sits ~9 px off every dot in the
    dive, because it was fitted from one frame that happened to lie elsewhere."""
    dots = _perpendicular(_dots_on_ray(), 9.4)
    with pytest.raises(CalibrationDoesNotDescribeDiveError, match="median"):
        check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


def test_a_rotated_ray_is_refused():
    """A mid-dive re-seat: the stored axis is a fraction of a degree off the one
    the measurement frames were shot with."""
    rotated = AXIS + np.array([0.004, -0.004, 0.0])
    dots = _dots_on_ray()
    with pytest.raises(CalibrationDoesNotDescribeDiveError):
        check_calibration_describes_dive(ORIGIN, rotated, K, dots)


def test_a_wild_minority_is_caught_by_the_p90_even_when_the_median_is_fine():
    """Dive 498's shape: median 5.4 px but p90 29.7. A median-only bound would
    let a partial disagreement through."""
    dots = _dots_on_ray()
    dots[-8:] = _perpendicular(dots, 40.0)[-8:]
    with pytest.raises(CalibrationDoesNotDescribeDiveError, match="p90"):
        check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


def test_a_disagreeing_subset_is_refused_once_it_is_a_real_population():
    """Dive 498's true proportions: 13 of its 40 live dots sit beyond the p90
    bound. The floor must not be so high that this stops being refused."""
    dots = _dots_on_ray(depths=np.linspace(0.6, 3.5, 40))
    dots[-13:] = _perpendicular(dots, 40.0)[-13:]
    with pytest.raises(CalibrationDoesNotDescribeDiveError, match="p90"):
        check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


# --- the p90 branch needs a real subset, not one or two labels --------------
#
# The dots come from the whole dive, so they include labels the fit never saw
# and labels nobody has checked yet. The 3 sigma per-dive validator that would
# supersede a reflection mislabel only runs on dives whose laser labelling is
# *complete*, so during labelling the population is unpoliced -- and because a
# recorded refusal self-expires the moment any label on the dive changes, a
# refusal earned this way comes back every hour a labeler works.


def test_a_pair_of_mislabels_does_not_refuse_a_fit_they_never_fed():
    """Two reflection mislabels in a partly-labelled dive. `np.percentile`
    interpolates, so at N=18 the 90th percentile already reaches into the two
    worst values and lands at ~18 px with the fit itself exact."""
    dots = _dots_on_ray(depths=np.linspace(0.6, 3.5, 18))
    dots[-2:] = _perpendicular(dots, 60.0)[-2:]
    check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


def test_one_wild_label_does_not_refuse_at_the_minimum_dot_count():
    """At N=6 the 90th percentile is effectively the maximum, while the bounds
    were measured on populations of 13 to 321 dots."""
    dots = _dots_on_ray(depths=np.linspace(0.6, 3.5, 6))
    dots[-1] = _perpendicular(dots, 60.0)[-1]
    check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


def test_the_median_branch_still_fires_below_the_disagreeing_dot_floor():
    """The floor is on the p90 branch only. A dive whose every dot disagrees
    is refused however few dots it has — that is dive 347, and the whole
    point of the gate."""
    dots = _perpendicular(_dots_on_ray(depths=np.linspace(0.6, 3.5, 6)), 9.4)
    with pytest.raises(CalibrationDoesNotDescribeDiveError, match="median"):
        check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


# --- abstention, so the gate never fires on data that cannot answer ---------


@pytest.mark.parametrize("n", [0, 1, 2, 5])
def test_abstains_on_too_few_dots(n):
    """Same posture as `check_fit_self_consistency`: too little to judge means
    say nothing, not refuse. A dive with a handful of dots says nothing about
    the fit; what exposes a fit that had nothing to constrain it is
    `check_observation_geometry`, on the lever arm of the observations — not
    a count, which is the quantity dive 347 satisfied with a duplicate."""
    dots = _perpendicular(_dots_on_ray(depths=np.linspace(0.6, 3.5, max(n, 1))), 60.0)
    check_calibration_describes_dive(ORIGIN, AXIS, K, dots[:n])


def test_abstains_when_the_dots_do_not_span_enough_pixels():
    """All the dots at one range define no direction, so a perpendicular offset
    is not measurable against them."""
    dots = _perpendicular(_dots_on_ray(depths=np.full(30, 1.2)), 60.0)
    check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


def test_nan_and_infinite_dots_are_ignored_rather_than_poisoning_the_statistic():
    dots = _dots_on_ray()
    dots[3] = [np.nan, np.nan]
    dots[7] = [np.inf, 0.0]
    check_calibration_describes_dive(ORIGIN, AXIS, K, dots)


# --- the bounds themselves --------------------------------------------------


def test_bounds_sit_between_the_measured_populations():
    """Healthy dives reach median 2.27 px (dive 509) with the two ambiguous
    ones at 3.33 and 3.92 (341, 349); the bad ones are 5.37 and 9.42 (498,
    347). Healthy p90 reaches 6.07 (341) against 18.44 and 29.69 for the bad.
    Widening past these re-admits a known-bad fit; tightening refuses dives
    whose calibrations are merely imperfect."""
    assert 3.92 < DEFAULT_MAX_DIVE_MEDIAN_OFFSET_PX < 9.42
    assert 6.07 < DEFAULT_MAX_DIVE_P90_OFFSET_PX < 18.44


def test_the_disagreeing_dot_floor_sits_between_the_measured_populations():
    """Re-measured 2026-09-13 over all 32 stored calibrations against their
    dives' live dots: **every one of the 30 sound calibrations has zero dots
    beyond the p90 bound.** The two refused have 13 (dive 498, of 40) and 139
    (dive 347, of 321). So the floor has only to clear the handful of stray
    labels a dive accumulates mid-labelling and stay below 13."""
    assert 2 < MIN_DISAGREEING_DIVE_DOTS < 13


def test_error_names_the_dive_statistic_so_a_refusal_is_actionable():
    dots = _perpendicular(_dots_on_ray(), 9.4)
    with pytest.raises(CalibrationDoesNotDescribeDiveError) as exc:
        check_calibration_describes_dive(ORIGIN, AXIS, K, dots)
    message = str(exc.value)
    assert "9.4" in message or "9.3" in message
    assert "px" in message
    assert str(int(DEFAULT_MAX_DIVE_MEDIAN_OFFSET_PX)) in message
