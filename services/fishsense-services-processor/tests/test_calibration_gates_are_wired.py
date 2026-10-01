"""Both fits must actually call the gates, before anything is accepted.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_calibration_gates_are_wired.py and
test_calibration_refusal_is_recorded.py.

The gate suites test the gates as pure functions, which says nothing about
whether anything calls them. Deleting a call site — or moving it after the
result is built — leaves those suites entirely green while the bad
calibration lands in the database and becomes borrowable by sibling dives
through `calibration_source_dive_id`.

So these drive the checkerboard fit end to end with a fit the gate must
refuse, and assert that it comes back **refused**, naming the dive's problem,
with nothing to persist -- and, the complement, that a sound fit comes back
accepted with no refusal, so the feature cannot "pass" by refusing everything.

v2 changes: the fit returns its refusal (v1 recorded it through the SDK and
raised a non-retryable `ApplicationError`). The orchestrator records it and
raises non-retryably, and pins that a failure to record does not mask the
refusal (v1's `test_a_failure_to_record_does_not_mask_the_refusal`, now in the
orchestrator's `test_calibration_persist`). The dive id is not in the reason
any more -- the orchestrator's error names the dive; the reason is what the
row stores against it.
"""

from __future__ import annotations

import inspect
import uuid

import numpy as np
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.slate_calibration import CheckerboardObservation
from fishsense_services_processor.calibration import fit as fit_module
from fishsense_services_processor.checkerboard import activities as board_module
from fishsense_services_processor.checkerboard.activities import (
    FitCheckerboardExtrinsicsInput,
)

from ._calibration_fixtures import dive_dots_on_ray

CAMERA_MATRIX = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]

#: 2.35 cm — dive 522's real fitted baseline, and the worst of the eight.
_IMPLAUSIBLE_XY = (0.0141, 0.0188)
#: 10.4 cm, where every sound calibration in the fleet sits.
_PLAUSIBLE_XY = (0.0624, 0.0832)


def _observations(n: int = 12, *, depth=None) -> list[CheckerboardObservation]:
    """Enough well-spread observations that nothing else refuses first."""
    return [
        CheckerboardObservation(
            capture_id=uuid.UUID(int=100 + i),
            point=[0.01 * i, 0.02, 1.0 + 0.1 * i if depth is None else depth],
            laser_x=600.0 + 40 * i,
            laser_y=500.0 + 30 * i,
        )
        for i in range(n)
    ]


def _payload(origin_xy, *, dive_offset_px=0.0, observations=None):
    """The dive's own dots are built on the projection of the ray the fake
    kernel returns, so `check_calibration_describes_dive` passes unless a test
    asks for a `dive_offset_px` — the honest stand-in for "the whole dive
    agrees"."""
    return FitCheckerboardExtrinsicsInput(
        dive_id=uuid.UUID(int=522),
        camera_matrix=CAMERA_MATRIX,
        observations=_observations() if observations is None else observations,
        dive_dots=dive_dots_on_ray(
            (*origin_xy, 0.0), (0.0, 0.0, 1.0), CAMERA_MATRIX, offset_px=dive_offset_px
        ),
    )


def _patch_fit(monkeypatch, origin_xy) -> None:
    """Force the kernel's answer. The axis points straight down the optical
    axis and self-consistency is stubbed, so only the gate under test fires."""
    monkeypatch.setattr(
        fit_module,
        "_calibrate_laser",
        lambda _points: (np.array(origin_xy), np.array([0.0, 0.0, 1.0])),
    )
    monkeypatch.setattr(
        fit_module, "check_fit_self_consistency", lambda *a, **k: "passed"
    )


async def _fit(payload):
    return await ActivityEnvironment().run(
        board_module.fit_checkerboard_laser_extrinsics, payload
    )


async def test_checkerboard_fit_refuses_an_implausible_baseline(monkeypatch):
    _patch_fit(monkeypatch, _IMPLAUSIBLE_XY)

    result = await _fit(_payload(_IMPLAUSIBLE_XY))

    assert result.outcome == "refused"
    assert result.refusal_type == "CalibrationImplausibleError"
    assert "baseline" in result.refusal_reason.lower()
    assert result.laser_position is None
    assert result.gate_verdicts["baseline_plausible"] == "refused"
    assert result.gate_verdicts["describes_dive"] == "not_run"


async def test_checkerboard_fit_accepts_a_plausible_baseline(monkeypatch):
    """The complement, so the gate cannot be 'passed' by refusing everything."""
    _patch_fit(monkeypatch, _PLAUSIBLE_XY)

    result = await _fit(_payload(_PLAUSIBLE_XY))

    assert result.outcome == "accepted"
    assert result.refusal_reason is None


async def test_checkerboard_fit_refuses_a_fit_the_dive_disagrees_with(monkeypatch):
    """The gate the other three cannot stand in for.

    Baseline and self-consistency both look only at the fit's own
    observations, so a board burst that is internally perfect but was shot
    with the laser in a different state from the dive's fish frames passes
    them both. Prod dive 347's calibration sits 9.4 px off its own 321 dots
    and was caught by nothing.
    """
    _patch_fit(monkeypatch, _PLAUSIBLE_XY)

    result = await _fit(_payload(_PLAUSIBLE_XY, dive_offset_px=40.0))

    assert result.outcome == "refused"
    assert result.refusal_type == "CalibrationDoesNotDescribeDiveError"


async def test_checkerboard_fit_refuses_a_single_distance_burst(monkeypatch):
    """A board burst at one distance determines the ray's direction no better
    than one frame does, however many corners it detects — and it is the
    geometry under which every other gate abstains."""
    _patch_fit(monkeypatch, _PLAUSIBLE_XY)

    result = await _fit(
        _payload(_PLAUSIBLE_XY, observations=_observations(12, depth=1.40))
    )

    assert result.outcome == "refused"
    assert result.refusal_type == "CalibrationUnderdeterminedError"
    assert result.gate_verdicts["observation_geometry"] == "refused"


async def test_too_few_observations_is_a_refusal(monkeypatch):
    _patch_fit(monkeypatch, _PLAUSIBLE_XY)

    result = await _fit(_payload(_PLAUSIBLE_XY, observations=_observations(1)))

    assert result.outcome == "refused"
    assert "insufficient" in result.refusal_reason.lower()


def test_the_fit_gates_before_it_accepts():
    """Source-order check on the shared fit, which both producers call.

    Crude, and deliberately so: the property is that every gate call precedes
    the accepted result in the function body. The failure this guards against
    — someone moving a call below the result — is exactly a textual
    reordering.
    """
    source = inspect.getsource(fit_module.fit_laser)
    accepted_at = source.index('outcome="accepted"')
    for gate in (
        "check_observation_geometry(",
        "check_fit_self_consistency(",
        "check_baseline_plausible(laser_position)",
        "check_calibration_describes_dive(",
    ):
        assert source.index(gate) < accepted_at, gate


def test_both_producers_fit_through_the_gated_function():
    """v1 had two copies of the fit; the gates had to be wired into both.
    Here both producers call `fit_laser`, so wiring one wires the other."""
    from fishsense_services_processor.laser_calibration import activities as slate

    assert "fit_laser(" in inspect.getsource(slate.perform_laser_calibration)
    assert "fit_laser(" in inspect.getsource(
        board_module.fit_checkerboard_laser_extrinsics
    )
