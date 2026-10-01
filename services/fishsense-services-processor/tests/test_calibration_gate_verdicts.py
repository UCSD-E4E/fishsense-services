"""v2: each gate says what it did, so the calibration row can record it.

v1's gates returned None whether they judged or abstained, so a stored
calibration could not say which gates actually looked at it. PLAN.md §4.3 asks
the laser calibration to record "each gate verdict"
(`laser_calibrations.gate_verdicts`), and an abstention is exactly the case
worth knowing about afterwards: dive 347's fit passed the self-consistency
gate by abstaining on one dot. A refusal still raises, as in v1 (the other
suites pin that); these pin the value returned when a gate does not refuse.
"""

from __future__ import annotations

import numpy as np

from fishsense_services_processor.calibration.consistency import (
    ABSTAINED,
    PASSED,
    check_baseline_plausible,
    check_calibration_describes_dive,
    check_fit_self_consistency,
    check_observation_geometry,
)

K = np.array([[2840.0, 0.0, 2000.0], [0.0, 2860.0, 1450.0], [0.0, 0.0, 1.0]])
ORIGIN = np.array([-0.031, -0.099, 0.0])
AXIS = np.array([0.01, 0.03, 0.999]) / np.linalg.norm([0.01, 0.03, 0.999])


def _dots(depths) -> np.ndarray:
    ts = (np.asarray(depths, float) - ORIGIN[2]) / AXIS[2]
    points = ORIGIN[None, :] + ts[:, None] * AXIS[None, :]
    homogeneous = (K @ points.T).T
    return homogeneous[:, :2] / homogeneous[:, 2:3]


def _points(depths) -> np.ndarray:
    ts = (np.asarray(depths, float) - ORIGIN[2]) / AXIS[2]
    return ORIGIN[None, :] + ts[:, None] * AXIS[None, :]


def test_a_judged_fit_is_reported_as_passed():
    depths = np.linspace(0.6, 3.0, 20)
    assert check_observation_geometry(_points(depths)) == PASSED
    assert check_fit_self_consistency(ORIGIN, AXIS, K, _dots(depths)) == PASSED
    assert check_baseline_plausible([0.0624, 0.0832, 0.0]) == PASSED
    assert check_calibration_describes_dive(ORIGIN, AXIS, K, _dots(depths)) == PASSED


def test_an_abstention_is_reported_as_abstained_not_passed():
    """Dive 347's shape: one dot is too few to define a line, so the
    projection gates say nothing -- and the row should say they said nothing."""
    one = _dots([1.2])
    assert check_fit_self_consistency(ORIGIN, AXIS, K, one) == ABSTAINED
    assert check_calibration_describes_dive(ORIGIN, AXIS, K, one) == ABSTAINED


def test_the_verdicts_are_distinct_strings():
    assert {PASSED, ABSTAINED} == {"passed", "abstained"}
