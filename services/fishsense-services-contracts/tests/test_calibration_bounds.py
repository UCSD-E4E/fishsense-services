"""Unit tests for the shared laser-baseline bounds.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
test_calibration_bounds.py; bodies and reasons are v1's. v2 change: the module
is ``fishsense_services_contracts.calibration_bounds``. The processor's gates
read it, and the API's SQL function ``plausible_laser_baseline`` (migration
0018) spells the same two numbers, pinned against these by the API's
``test_laser_calibration_store``.

The baseline is the offset between camera and laser on one rig — hardware, not
a property of a dive. Over all 35 stored calibrations its interquartile range
is 9.99-10.45 cm, half a centimetre across both producers, every camera and two
years. Eight fits sit outside 9.7-14.5 cm and back 663 of 3,104 measurements at
-75% to +45% error, and two more (8.90, 9.51 cm) hide a -14 to -18 % close-range
error behind a flat-looking median.
"""

from __future__ import annotations

import math

import pytest

from fishsense_services_contracts.calibration_bounds import (
    MAX_BASELINE_M,
    MIN_BASELINE_M,
    baseline_m,
    is_plausible_baseline,
)


def test_baseline_is_the_in_plane_norm():
    assert baseline_m([0.06, 0.08, 0.0]) == pytest.approx(0.10)


def test_the_z_component_is_ignored():
    """Both producers pad z to zero on an origin that is the z=0 crossing.

    Reading a three-component norm would make the answer depend on padding
    that carries no information, and would drift if that convention changed.
    """
    assert baseline_m([0.06, 0.08, 5.0]) == pytest.approx(0.10)


@pytest.mark.parametrize(
    "position",
    [
        [],
        [0.06],
        None,
        "not a position",
        [None, None],
        ["a", "b"],
        {"x": 1},
    ],
)
def test_unreadable_positions_report_infinite(position):
    """ "Cannot tell" and "implausible" want the same treatment: refuse.

    Returning `inf` rather than raising means a caller filtering a table of
    stored rows cannot be derailed by one malformed entry.
    """
    assert baseline_m(position) == math.inf


def test_nan_reports_infinite_rather_than_nan():
    """NaN fails every comparison, so a caller's range test would ACCEPT it."""
    assert baseline_m([float("nan"), 0.0]) == math.inf


def test_infinite_input_is_not_plausible():
    assert not is_plausible_baseline([float("inf"), 0.0])


@pytest.mark.parametrize("baseline_cm", [9.87, 10.12, 10.36, 10.45, 12.95])
def test_accepts_the_baselines_the_fleet_actually_produces(baseline_cm):
    """Measured values from calibrations whose lengths hold flat across range."""
    assert is_plausible_baseline([baseline_cm / 100, 0.0, 0.0])


@pytest.mark.parametrize(
    "baseline_cm", [2.35, 2.60, 4.81, 4.90, 6.25, 6.91, 8.90, 9.51, 16.01, 22.22]
)
def test_refuses_the_baselines_that_produced_bad_lengths(baseline_cm):
    """The known-bad fits, by their real measured baselines.

    8.90 and 9.51 cm were accepted until 2026-09-12. Their median length error
    was ~-1 %, which looked fine, but the range trend of a rigid target showed
    why: -14 to -18 % at 0.8 m rising to ~0 at 4 m -- a short baseline's flat
    scale error and a compensating angle error that cancel where the median
    sits. Parametrised on the actual numbers so that widening the bounds far
    enough to re-admit one fails the build.
    """
    assert not is_plausible_baseline([baseline_cm / 100, 0.0, 0.0])


def test_a_zero_baseline_is_not_plausible():
    """A laser at the camera centre is not a rig, it is a failed fit."""
    assert not is_plausible_baseline([0.0, 0.0, 0.0])


def test_the_bounds_are_inclusive_at_the_edges():
    assert is_plausible_baseline([MIN_BASELINE_M, 0.0])
    assert is_plausible_baseline([MAX_BASELINE_M, 0.0])


def test_overrides_are_honoured():
    """So a caller can tighten for an experiment without editing the module."""
    assert not is_plausible_baseline(
        [0.104, 0.0], min_baseline_m=0.11, max_baseline_m=0.12
    )


def test_bounds_bracket_the_measured_cluster():
    """The constants themselves, so widening them is a deliberate act.

    The fleet sits at 9.99-10.45 cm interquartile. The bounds must contain that
    comfortably while still excluding the nearest bad fits on each side —
    9.51 cm below (dive 498, shown wrong by its range trend) and 16.01 cm
    above — and sit nearer the midpoint than hard against the healthy
    extremes (9.87 and 12.95 cm), or ordinary variation gets refused.
    """
    assert MIN_BASELINE_M < 0.0987
    assert MIN_BASELINE_M > 0.0951
    assert MAX_BASELINE_M > 0.1295
    assert MAX_BASELINE_M < 0.1601
