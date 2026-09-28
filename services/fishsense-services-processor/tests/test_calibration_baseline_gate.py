"""The baseline plausibility gate — the one check that sees the failing axis.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_calibration_baseline_gate.py.

`check_fit_self_consistency` compares the fitted ray's *projection* against the
2D dots it came from. Many different 3D rays project to the same image line —
the whole family lying in the plane through the camera centre and that line —
and sliding the laser's offset along that family moves where the ray crosses
z=0 while leaving the projection identical. So that gate constrains two of the
ray's four degrees of freedom and the baseline sits in the two it cannot see.
It is the epipolar blindness already documented for reprojection residual and
scale, in a second guise.

Measured 2026-09-11 over all 35 stored calibrations: the baseline is a rig
constant. Its interquartile range is 9.99–10.45 cm — half a centimetre across
both producers, every camera and two years — while 8 fits sit outside 8–13 cm,
at 2.35, 2.60, 4.81, 4.90, 6.25, 6.91, 16.01 and 22.22 cm. Those 8 back 12
dives and 663 of 3,104 measurements, and their length errors run -75% to +45%.

Two of the eight are **slate**-derived, which is why this gate lives beside
`check_fit_self_consistency` rather than in the checkerboard path: both
producers feed the same fit and both can miss the same way.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.calibration.consistency import (
    DEFAULT_MAX_BASELINE_M,
    DEFAULT_MIN_BASELINE_M,
    CalibrationImplausibleError,
    check_baseline_plausible,
)


def _position(baseline_m: float) -> np.ndarray:
    """A laser position with the given offset from the camera centre.

    The fit returns the origin with z implicit at 0 (the crossing of the
    camera plane), so the baseline is the norm of the x/y pair.
    """
    return np.array([baseline_m * 0.6, baseline_m * 0.8, 0.0])


@pytest.mark.parametrize("baseline_cm", [9.87, 10.12, 10.36, 10.45, 12.95])
def test_accepts_every_baseline_the_fleet_actually_produces(baseline_cm):
    """Real good fits must pass, or the gate is useless in practice.

    These are measured values from prod calibrations whose lengths grade
    within -12.7%, including the two extremes of the healthy band.
    """
    check_baseline_plausible(_position(baseline_cm / 100))


@pytest.mark.parametrize(
    "baseline_cm", [2.35, 2.60, 4.81, 4.90, 6.25, 6.91, 16.01, 22.22]
)
def test_refuses_every_baseline_that_produced_bad_lengths(baseline_cm):
    """The eight known-bad fits, by their measured baselines.

    Parametrised on the real numbers rather than invented ones so the gate is
    pinned against the population it was derived from — if someone widens the
    bound far enough to re-admit these, this fails.
    """
    with pytest.raises(CalibrationImplausibleError):
        check_baseline_plausible(_position(baseline_cm / 100))


def test_the_error_names_the_measured_baseline_and_the_bound():
    """An operator reading the log must be able to tell how far off it was."""
    with pytest.raises(CalibrationImplausibleError) as excinfo:
        check_baseline_plausible(_position(0.0235))

    message = str(excinfo.value)
    assert "2.35" in message or "0.0235" in message
    assert "refus" in message.lower()


def test_a_zero_baseline_is_refused():
    """A laser at the camera centre is not a rig, it is a failed fit."""
    with pytest.raises(CalibrationImplausibleError):
        check_baseline_plausible(np.array([0.0, 0.0, 0.0]))


def test_a_non_finite_baseline_is_refused():
    """NaN comparisons are all False, so a naive bound would ACCEPT this."""
    with pytest.raises(CalibrationImplausibleError):
        check_baseline_plausible(np.array([float("nan"), 0.0, 0.0]))


def test_the_z_component_is_ignored():
    """The fit returns the origin at the z=0 crossing and pads z.

    Reading a three-component norm here would make the gate depend on padding
    that carries no information, and would drift if that convention changed.
    """
    check_baseline_plausible(np.array([0.104, 0.0, 5.0]))


def test_bounds_bracket_the_measured_fleet_cluster():
    """The constants themselves, so widening them is a deliberate act.

    The fleet sits at 9.99–10.45 cm interquartile. The bound must contain that
    comfortably and still exclude 9.51 cm and 16.01 cm, the nearest bad fits on
    each side.
    """
    assert DEFAULT_MIN_BASELINE_M < 0.0987
    assert DEFAULT_MIN_BASELINE_M > 0.0951
    assert DEFAULT_MAX_BASELINE_M > 0.1296
    assert DEFAULT_MAX_BASELINE_M < 0.1600
