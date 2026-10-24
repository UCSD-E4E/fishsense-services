"""`fit_laser` on fishsense-core's guards (`fishsense_core.calibration_checks`).

The gates and the outlier trim used to be this package's own copies
(`calibration/consistency.py`, `calibration/robust_fit.py`); fishsense-core
4.3.0 owns them now (core #99). Core's suites test the guards as pure
functions. These pin what the move changes at services' one call site, through
the real Rust kernel rather than a stub:

- a non-finite observation (a failed pose or depth) is dropped by core's trim
  and counted as trimmed. The services trim kept it, the kernel fitted a NaN
  ray, and the self-consistency gate's SVD raised `LinAlgError`: an activity
  failure, retried, rather than a fit. Neither producer hands the fit such a
  row today (both skip observations without a point), so no stored row
  changes;
- the bounds and remedy text are services' own, passed at the call site.
"""

from __future__ import annotations

import inspect

import numpy as np

from fishsense_services_contracts.calibration_bounds import (
    MAX_BASELINE_M,
    MIN_BASELINE_M,
)
from fishsense_services_processor.calibration import fit as fit_module

K = np.array([[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]])


def _scene(n: int = 12):
    """Collinear points over 1.0-2.1 m whose ray crosses z=0 at a 10.2 cm
    baseline (x = -0.10 m, y = 0.02 m), and their exact pixels."""
    i = np.arange(n)
    points = np.column_stack([0.01 * i, np.full(n, 0.02), 1.0 + 0.1 * i])
    homogeneous = (K @ points.T).T
    return points, homogeneous[:, :2] / homogeneous[:, 2:3]


def _fit(points, dots, mission_dots):
    return fit_module.fit_laser(
        points,
        dots,
        K,
        mission_dots,
        too_few_type="InsufficientLaserPoints",
        too_few_reason=lambda n, m: f"{n} < {m}",
    )


def test_a_clean_scene_is_accepted_through_the_real_kernel():
    """The control: a known answer, so the test below cannot pass by
    accepting nothing."""
    points, dots = _scene()

    result = _fit(points, dots, dots)

    assert result.outcome == "accepted", result.refusal_reason
    assert result.observations_trimmed == 0
    assert np.allclose(result.laser_position, [-0.10, 0.02, 0.0], atol=1e-4)


def test_a_non_finite_observation_is_dropped_not_fitted():
    points, dots = _scene()
    points = np.vstack([points, [np.nan, np.nan, np.nan]])
    dots = np.vstack([dots, [700.0, 500.0]])

    result = _fit(points, dots, dots[:-1])

    assert result.outcome == "accepted", result.refusal_reason
    assert result.observation_count == 13
    assert result.observations_trimmed == 1
    assert np.allclose(result.laser_position, [-0.10, 0.02, 0.0], atol=1e-4)


def test_the_baseline_bounds_are_the_contracts():
    """The api excludes a stored calibration on the same numbers, so the gate
    must read the contract's, not core's copy of them."""
    source = inspect.getsource(fit_module.fit_laser)
    assert "min_baseline_m=MIN_BASELINE_M" in source
    assert "max_baseline_m=MAX_BASELINE_M" in source
    assert fit_module.MIN_BASELINE_M is MIN_BASELINE_M
    assert fit_module.MAX_BASELINE_M is MAX_BASELINE_M


def test_every_gate_is_given_the_remedies():
    """Core's gates append nothing unless told; the refusal-message suite pins
    the resulting text, this pins that no gate call omits it."""
    source = inspect.getsource(fit_module.fit_laser)
    for gate in (
        "check_observation_geometry(",
        "check_fit_self_consistency(",
        "check_baseline_plausible(",
        "check_calibration_describes_mission(",
    ):
        call = source[source.index(gate) :]
        call = call[: call.index("),")]
        assert "remedies=REFUSAL_REMEDIES" in call, gate
