"""`MIN_LASER_POINTS` counts observations; it does not ask where they are.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_calibration_observation_geometry.py.

Stage 13 fits the laser ray from 3-D points, each the camera ray through a
labelled dot intersected with the target plane. What determines the fitted
*direction* is the lever arm — the spread of those points along the ray —
against the label noise. One pixel of dot-label noise at range z is z/f metres
of lateral error, so it rotates the fitted ray by about (z/f)/lever radians,
and the measured sensitivity of length to that angle is ~30 % per degree at
2 m. The observation count says nothing about this: sixteen dots at one range
determine the direction no better than two.

Measured over the 32 stored calibrations whose observations are recoverable
(2026-09-13), lever arm = depth spread of the extreme observations:

    dive 341   30 obs   1.55 m   ->  0.1 % of length per px of label noise
    dive 471    2 obs   1.56 m   ->  0.7 %
    dive 279    3 obs   1.47 m   ->  0.6 %
    dive 465    3 obs   1.34 m   ->  0.6 %
    dive 383    2 obs   1.09 m   ->  0.7 %
    dive 062   20 obs   1.02 m   ->  0.4 %     <- tightest sound calibration
    dive 349    2 obs   0.26 m   ->  5.8 %     <- refused here
    dive 107   16 obs   0.03 m   ->  5.5 %     <- refused here
    dive 347    1 obs   0.00 m   ->  degenerate

Two observations over a metre of range are worth more than sixteen at one
distance, which is why this gate bounds the lever arm and not the count. The
bound sits between the populations (1.02 m healthy, 0.26 m bad), nearer the
bad side because a refusal wedges the dive in its cohort.

Dive 107 is the case that shows the existing gates cannot do this: sixteen
observations, a 12.95 cm baseline that `check_baseline_plausible` accepts as
the fleet's high extreme, a dot span wide enough that
`check_fit_self_consistency` does not abstain — and a 2.8 cm lever arm
(1.97-2.00 m). Dive 526 had the same single-distance geometry at 6.6 cm
(4.16-4.22 m) and was only caught because its fit happened to collapse to a
2.00 cm baseline.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.calibration.consistency import (
    MIN_OBSERVATION_LEVER_M,
    CalibrationUnderdeterminedError,
    check_observation_geometry,
)

AXIS = np.array([0.00737, 0.03204, 0.99946])


def _observations(depths, origin=(-0.0306, -0.0977, 0.0)) -> np.ndarray:
    """3-D observation points along the laser ray at the given depths — the
    shape `_gather_laser_points` hands to the fit."""
    origin = np.asarray(origin, float)
    axis = AXIS / np.linalg.norm(AXIS)
    ts = (np.asarray(depths, float) - origin[2]) / axis[2]
    return origin[None, :] + ts[:, None] * axis[None, :]


# --- accepted ---------------------------------------------------------------


def test_two_observations_over_a_wide_range_are_enough():
    """Dive 471's geometry: two dots, 1.56 m apart. Well determined."""
    check_observation_geometry(_observations([1.12, 2.68]))


def test_a_long_burst_over_a_wide_range_is_accepted():
    check_observation_geometry(_observations(np.linspace(1.19, 2.74, 30)))


def test_the_tightest_healthy_dive_is_accepted():
    """Dive 62: 1.02 m of lever over 20 observations — the closest sound
    calibration to the bound, so this pins that the bound does not refuse it.
    Dive 383's 1.09 m is the next tightest and is covered by the same margin."""
    check_observation_geometry(_observations(np.linspace(1.38, 2.40, 20)))


# --- refused ----------------------------------------------------------------


def test_a_single_distance_burst_is_refused_however_many_observations():
    """Dive 107: sixteen observations inside 3 cm of range. Every other gate
    passes it; its 12.95 cm baseline is the fleet's highest."""
    rng = np.random.default_rng(0)
    depths = 1.97 + rng.uniform(0.0, 0.03, 16)
    with pytest.raises(CalibrationUnderdeterminedError, match="lever"):
        check_observation_geometry(_observations(depths))


def test_two_close_observations_are_refused():
    """Dive 349: two dots 26 cm apart at 2.4 m — 5.8 % of length per pixel."""
    with pytest.raises(CalibrationUnderdeterminedError):
        check_observation_geometry(_observations([2.361, 2.622]))


def test_a_single_observation_is_refused():
    with pytest.raises(CalibrationUnderdeterminedError):
        check_observation_geometry(_observations([1.197]))


def test_coincident_observations_are_refused():
    """Two labels at the identical pixel on one frame are one observation."""
    with pytest.raises(CalibrationUnderdeterminedError):
        check_observation_geometry(_observations([1.2, 1.2]))


def test_empty_is_refused_rather_than_crashing():
    with pytest.raises(CalibrationUnderdeterminedError):
        check_observation_geometry(np.zeros((0, 3)))


# --- the bound and the message ---------------------------------------------


def test_bound_sits_between_the_measured_populations():
    """Healthy reaches down to 1.02 m (dive 62, 20 observations); the bad ones
    are at 0.26 m and below (349, 107, 347). Nearer the bad side: a refusal is
    expensive. Pinned at 1.02 and not 1.09, so that raising the bound past the
    tightest *sound* calibration cannot pass with both tests green."""
    assert 0.26 < MIN_OBSERVATION_LEVER_M < 1.02


def test_the_message_reports_the_lever_and_the_count():
    with pytest.raises(CalibrationUnderdeterminedError) as exc:
        check_observation_geometry(_observations([2.361, 2.622]))
    message = str(exc.value)
    assert "0.26" in message
    assert "2 observation" in message
    assert "lever" in message


def test_non_finite_observations_are_dropped_not_counted():
    obs = _observations([1.12, 2.68, 1.9])
    obs[2] = [np.nan, np.nan, np.nan]
    check_observation_geometry(obs)
    obs2 = _observations([2.361, 2.622, 1.0])
    obs2[2] = [np.inf, 0.0, 0.0]
    with pytest.raises(CalibrationUnderdeterminedError):
        check_observation_geometry(obs2)
