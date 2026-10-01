"""The scale-free range-trend audit.

A laser calibration whose in-plane angle is off by eps radians misreads depth
by a factor (1 + eps * z / b), b the baseline -- linear in range. Every other
check in the pipeline is blind to that angle (reprojection residual, the
baseline plausibility gate, the dot-on-board depth check), because the image
of the laser line fixes only the plane containing it. The trend of measured
length against range is not blind to it, and it needs no known length: a
rigid object has to read the same length at every range, so the relative
slope of length against depth is the diagnostic and slope * b is the angle.

These tests pin the estimator against exact synthetic data, the data
requirements the audit refuses to work without, and the two things measured
on the corpus that decide how it may be used: every cell whose interval
cleared +-2 %/m was a calibration already known bad, in both directions --
negative for a rotated axis, positive for a short fitted baseline paired with
a compensating angle -- and no sound-baseline cell was flagged.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_range_trend.py; names and bodies are v1's. v2
changes: a measurement is keyed by its `capture_id` (v1: `image_id`) and a
wild fish by its `fish_number`, v2's public identifier (v1: `fish_id`).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from fishsense_services_api.range_trend import (
    DEFAULT_MIN_DEPTH_M,
    DEFAULT_MIN_FRAMES,
    DEFAULT_MIN_RANGE_RATIO,
    FLAG_SLOPE_PCT_PER_M,
    RangeTrend,
    group_by_object,
    range_trend,
    theil_sen,
)

BASELINE_M = 0.103  # fleet value, see CLAUDE.md


def _phi_error_lengths(depths, true_length_m, eps_rad, baseline_m=BASELINE_M):
    """Lengths a rigid object of `true_length_m` reads under an in-plane
    calibration error `eps_rad`: depth, and so length, scales by
    (1 + eps * z / b)."""
    z = np.asarray(depths, dtype=float)
    return true_length_m * (1.0 + eps_rad * z / baseline_m)


# --- Theil-Sen ----------------------------------------------------------------


def test_theil_sen_is_exact_on_a_line():
    x = np.array([0.5, 1.0, 1.5, 2.0, 3.0])
    y = 2.0 + 0.7 * x
    slope, lo, hi = theil_sen(x, y)
    assert slope == pytest.approx(0.7)
    assert lo == pytest.approx(0.7)
    assert hi == pytest.approx(0.7)


def test_theil_sen_ignores_a_single_wild_point():
    x = np.arange(1, 21, dtype=float)
    y = 1.0 + 0.5 * x
    y[7] += 40.0
    slope, _, _ = theil_sen(x, y)
    assert slope == pytest.approx(0.5, abs=1e-9)


def test_theil_sen_confidence_interval_brackets_the_slope_and_narrows_with_n():
    rng = np.random.default_rng(0)
    x = np.linspace(0.5, 4.0, 60)
    y = 0.3 * x + rng.normal(0, 0.05, x.size)
    slope, lo, hi = theil_sen(x, y)
    assert lo < slope < hi
    assert lo < 0.3 < hi
    x2 = np.linspace(0.5, 4.0, 12)
    y2 = 0.3 * x2 + rng.normal(0, 0.05, x2.size)
    _, lo2, hi2 = theil_sen(x2, y2)
    assert (hi2 - lo2) > (hi - lo)


def test_theil_sen_refuses_fewer_than_three_points():
    with pytest.raises(ValueError):
        theil_sen(np.array([1.0, 2.0]), np.array([1.0, 2.0]))


# --- the audit ----------------------------------------------------------------


def test_range_trend_recovers_an_injected_in_plane_angle_without_a_known_length():
    depths = np.linspace(0.9, 3.0, 40)
    eps = np.radians(-0.25)  # dive 492's size of error
    lengths = _phi_error_lengths(depths, 0.31, eps)
    trend = range_trend(depths, lengths, baseline_m=BASELINE_M)
    assert isinstance(trend, RangeTrend)
    assert trend.n == 40
    # relative slope in %/m -> eps in degrees, exact under the linear model
    assert trend.eps_deg == pytest.approx(-0.25, abs=0.01)
    assert trend.slope_pct_per_m == pytest.approx(100 * eps / BASELINE_M, rel=0.05)
    # the true length was never passed in
    assert not hasattr(trend, "known_length_m")


def test_range_trend_is_flat_on_a_good_calibration():
    rng = np.random.default_rng(1)
    depths = np.linspace(0.85, 3.5, 50)
    lengths = 0.15 * (1 + rng.normal(0, 0.02, depths.size))  # 2 % frame noise
    trend = range_trend(depths, lengths, baseline_m=BASELINE_M)
    assert abs(trend.slope_pct_per_m) < 1.0
    assert trend.ci_pct_per_m[0] < 0 < trend.ci_pct_per_m[1]
    assert not trend.flagged


def test_range_trend_flags_both_signs():
    """A short fitted baseline pairs with a compensating angle error and reads
    as a POSITIVE trend (dives 503/504 at 8.90 cm: +5.3 %/m); a plain rotation
    the wrong way reads negative. Both are calibration errors."""
    depths = np.linspace(0.9, 3.0, 40)
    short = range_trend(
        depths, _phi_error_lengths(depths, 0.31, np.radians(-0.25)), BASELINE_M
    )
    long = range_trend(
        depths, _phi_error_lengths(depths, 0.31, np.radians(+0.25)), BASELINE_M
    )
    assert short.slope_pct_per_m < -FLAG_SLOPE_PCT_PER_M
    assert short.flagged
    assert long.slope_pct_per_m > FLAG_SLOPE_PCT_PER_M
    assert long.flagged
    assert "short" in long.note and "baseline" in long.note
    assert "rotation" in short.note or "error" in short.note


def test_range_trend_flag_requires_the_interval_to_exclude_the_threshold():
    """A steep point estimate on noisy data is not a finding."""
    rng = np.random.default_rng(2)
    depths = np.linspace(0.9, 2.0, 9)
    lengths = 0.31 * (1 + rng.normal(0, 0.08, depths.size))
    trend = range_trend(depths, lengths, baseline_m=BASELINE_M)
    lo, hi = trend.ci_pct_per_m
    assert trend.flagged == (hi < -FLAG_SLOPE_PCT_PER_M or lo > FLAG_SLOPE_PCT_PER_M)


def test_range_trend_drops_frames_closer_than_the_minimum_depth():
    """The corpus-wide close-range under-read (< 0.8 m) would masquerade as a
    positive trend; those frames are excluded before the fit, not after."""
    depths = np.array([0.3, 0.4, 0.5, 0.6] + list(np.linspace(0.9, 3.0, 20)))
    lengths = np.where(depths < 0.8, 0.31 * 0.94, 0.31)  # 6 % short up close
    trend = range_trend(depths, lengths, baseline_m=BASELINE_M)
    assert trend.n == 20
    assert trend.slope_pct_per_m == pytest.approx(0.0, abs=1e-9)
    assert DEFAULT_MIN_DEPTH_M == 0.8


def test_range_trend_returns_none_without_enough_frames_or_range_spread():
    depths = np.linspace(0.9, 3.0, DEFAULT_MIN_FRAMES - 1)
    assert range_trend(depths, np.full(depths.size, 0.31), BASELINE_M) is None
    narrow = np.linspace(1.0, 1.0 * (DEFAULT_MIN_RANGE_RATIO - 0.1), 30)
    assert range_trend(narrow, np.full(narrow.size, 0.31), BASELINE_M) is None
    wide = np.linspace(1.0, 1.0 * DEFAULT_MIN_RANGE_RATIO, 30)
    assert range_trend(wide, np.full(wide.size, 0.31), BASELINE_M) is not None


def test_range_trend_rejects_non_positive_inputs():
    depths = np.array([0.9, 1.5, -2.0, 2.5] + [3.0] * 10)
    lengths = np.full(depths.size, 0.31)
    with pytest.raises(ValueError):
        range_trend(depths, lengths, BASELINE_M)
    with pytest.raises(ValueError):
        range_trend(np.abs(depths), np.where(lengths > 0, 0.0, 1.0), BASELINE_M)


def test_flag_threshold_sits_at_the_measured_separation():
    """On the corpus (2026-09-12): every sound-baseline (10.3-10.5 cm) cell sat
    within -1.0..+0.9 %/m; the known-bad cells sat beyond -2.8 or +2.8 (the
    short-baseline dives at +5.3 / +5.5). The threshold is between them."""
    assert FLAG_SLOPE_PCT_PER_M == 2.0


# --- grouping measurements into objects ---------------------------------------


def _m(capture_id, length_m, fish_number=None):
    return SimpleNamespace(
        capture_id=capture_id, length_m=length_m, fish_number=fish_number
    )


def test_group_by_object_keys_rigid_targets_by_name_and_fish_by_id():
    measurements = [_m(1, 0.31), _m(2, 0.30), _m(3, 0.15), _m(4, 0.42, fish_number=9)]
    depths = {1: 1.0, 2: 2.0, 3: 1.5, 4: 2.5}
    names = {1: "Weasly Fish", 2: "Weasly Fish", 3: "Box", 4: None}
    groups = group_by_object(measurements, depths, names)
    assert groups == {
        "Weasly Fish": ([1.0, 2.0], [0.31, 0.30]),
        "Box": ([1.5], [0.15]),
        "fish 9": ([2.5], [0.42]),
    }


def test_group_by_object_drops_measurements_missing_depth_length_or_object():
    measurements = [_m(1, 0.31), _m(2, None), _m(3, 0.31), _m(4, 0.31)]
    depths = {1: 1.0, 2: 2.0, 3: 1.5}  # image 4 has no depth
    names = {1: "Box", 2: "Box", 3: None, 4: "Box"}  # image 3: no name, no fish
    assert group_by_object(measurements, depths, names) == {"Box": ([1.0], [0.31])}
