"""`fit_laser` on fishsense-core's guards (`fishsense_core.calibration_checks`).

The gates and the outlier trim used to be this package's own copies
(`calibration/consistency.py`, `calibration/robust_fit.py`); fishsense-core
4.3.0 owns them now (core #99). Core's suites test the guards as pure
functions. These pin what the move changes at services' one call site, through
the real Rust kernel rather than a stub:

- a non-finite observation (a failed pose or depth) is not an observation.
  The services trim kept it, the kernel fitted a NaN ray, and the
  self-consistency gate's SVD raised `LinAlgError`: an activity failure,
  retried, rather than a fit. `fit_laser` drops it with its dot before the
  minimum count, so too few real observations get the producer's own
  refusal, and neither `observation_count` nor `observations_trimmed` counts
  it (review of #351). Neither producer hands the fit such a row today (both
  skip observations without a point), so no stored row changes;
- the gates judge only what was fitted: the self-consistency gate gets the
  dots of the points the trim kept;
- a malformed dots payload reaches only the gate that reads it, so an earlier
  gate's refusal is still recorded;
- the bounds and remedy text are services' own, passed at the call site.
"""

from __future__ import annotations

import numpy as np
import pytest
from fishsense_core import calibration_checks as core_checks

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


def test_a_non_finite_observation_is_dropped_with_its_dot():
    points, dots = _scene()
    points = np.vstack([points, [np.nan, np.nan, np.nan]])
    dots = np.vstack([dots, [700.0, 500.0]])

    result = _fit(points, dots, dots[:-1])

    assert result.outcome == "accepted", result.refusal_reason
    assert result.observation_count == 12
    assert result.observations_trimmed == 0
    assert np.allclose(result.laser_position, [-0.10, 0.02, 0.0], atol=1e-4)


@pytest.mark.parametrize(
    "points",
    [
        [[np.nan] * 3, [np.inf, 0.0, 1.0]],
        [[0.0, 0.02, 1.0], [np.nan] * 3],
    ],
    ids=["none-finite", "one-finite"],
)
def test_too_few_finite_observations_get_the_producers_refusal(points):
    """Counted before the drop, two non-finite rows passed the minimum, and
    the kernel raised a bare ValueError on the empty array: no refusal
    recorded, the dive left wedged in its cohort. One finite row was refused
    by the geometry gate, with the wrong type and remedy."""
    result = _fit(np.array(points), [[700.0, 500.0]] * 2, [])

    assert result.outcome == "refused"
    assert result.refusal_type == "InsufficientLaserPoints"
    assert (
        result.refusal_reason
        == f"{len([p for p in points if np.isfinite(p).all()])} < 2"
    )


def _spy(monkeypatch, name):
    calls = []
    real = getattr(fit_module, name)

    def gate(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(fit_module, name, gate)
    return calls


def test_the_self_consistency_gate_sees_only_the_fitted_dots(monkeypatch):
    """An outlier the trim drops, and a non-finite row, take their dots with
    them: the gate asks whether the fit reproduces its own observations."""
    points, dots = _scene()
    points[5] += [0.03, 0.0, 0.0]
    points = np.vstack([points, [np.nan, np.nan, np.nan]])
    all_dots = np.vstack([dots, [700.0, 500.0]])
    calls = _spy(monkeypatch, "check_fit_self_consistency")

    result = _fit(points, all_dots, dots)

    assert result.observations_trimmed == 1, result
    ((args, _),) = calls
    assert np.array_equal(args[3], np.delete(dots, 5, axis=0))


def test_a_malformed_dots_payload_does_not_hide_an_earlier_refusal():
    """Observations at one distance are refused by the geometry gate before
    any gate reads the dots; reshaping them up front raised instead."""
    points = np.column_stack([0.01 * np.arange(6), np.full(6, 0.02), np.full(6, 1.0)])

    result = _fit(points, [1.0, 2.0, 3.0], [1.0, 2.0, 3.0])

    assert result.outcome == "refused"
    assert result.refusal_type == "CalibrationUnderdeterminedError"


@pytest.mark.parametrize("span", [0.0, 0.3, 0.5999, 0.6, 0.6001, 1.2])
def test_the_recorded_lever_arm_is_the_one_the_gate_judges(span):
    """`lever_arm_m` is services' copy of the gate's quantity; core does not
    expose its own. They must agree at the bound, and in the message."""
    points = np.column_stack(
        [np.zeros(3), np.zeros(3), [1.0, 1.0 + span / 2, 1.0 + span]]
    )
    lever = fit_module._lever_arm(points)  # pylint: disable=protected-access
    try:
        core_checks.check_observation_geometry(points)
    except core_checks.CalibrationUnderdeterminedError as exc:
        assert lever < core_checks.MIN_OBSERVATION_LEVER_M
        assert f"span only {lever:.2f} m" in str(exc)
    else:
        assert lever >= core_checks.MIN_OBSERVATION_LEVER_M


def test_the_baseline_bounds_are_the_contracts(monkeypatch):
    """The api excludes a stored calibration on the same numbers, so the gate
    must read the contract's, not core's copy of them."""
    points, dots = _scene()
    calls = _spy(monkeypatch, "check_baseline_plausible")

    _fit(points, dots, dots)

    ((_, kwargs),) = calls
    assert kwargs["min_baseline_m"] is MIN_BASELINE_M
    assert kwargs["max_baseline_m"] is MAX_BASELINE_M


def test_every_gate_is_given_the_remedies(monkeypatch):
    """Core's gates append nothing unless told; the refusal-message suite pins
    the resulting text, this pins that no gate call omits it."""
    points, dots = _scene()
    gates = (
        "check_observation_geometry",
        "check_fit_self_consistency",
        "check_baseline_plausible",
        "check_calibration_describes_mission",
    )
    calls = {gate: _spy(monkeypatch, gate) for gate in gates}

    assert _fit(points, dots, dots).outcome == "accepted"

    for gate in gates:
        ((_, kwargs),) = calls[gate]
        assert kwargs["remedies"] is fit_module.REFUSAL_REMEDIES, gate
