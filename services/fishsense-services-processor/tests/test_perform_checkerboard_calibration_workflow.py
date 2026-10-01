"""Workflow contract + fit-activity tests for checkerboard calibration.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_perform_checkerboard_calibration_workflow.py. The
detection kernel is covered by `test_checkerboard_detection.py` and the
geometry by `test_calibration_geometry.py`. What is left, and what these pin
down, is the wiring: that the fan-out hands every frame the board geometry it
was dispatched with, that unusable frames are dropped rather than failing the
dive, and that the fit refuses the same cases stage 13 refuses.

v2 changes, each pinned here:

* the fit **returns** a `LaserCalibrationResult` (v1: persisted through the
  SDK and returned the row id); a refusal comes back `refused`, with v1's
  error type and reason, for the orchestrator to record and raise;
* the dive's dots arrive in the payload (v1: fetched by the fit activity);
* the board is a `CheckerboardTarget` with its pitch per axis, and each frame
  is a raw `ObjectRef` (v1: a checksum);
* the fit and stage 13 share one implementation (`calibration.fit`), so "the
  same threshold" is now "the same function".
"""

from __future__ import annotations

import logging
import uuid

import numpy as np
import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import (
    CheckerboardCalibrationImage,
    CheckerboardObservation,
    CheckerboardTarget,
    LaserCalibrationResult,
    PerformCheckerboardCalibrationInput,
)
from fishsense_services_processor.calibration import fit as fit_module
from fishsense_services_processor.checkerboard import activities as sut
from fishsense_services_processor.checkerboard.activities import (
    DetectCheckerboardLaserPointInput,
    FitCheckerboardExtrinsicsInput,
)
from fishsense_services_processor.checkerboard.workflows import (
    PerformCheckerboardCalibrationWorkflow,
)

from ._calibration_fixtures import dive_dots_on_ray

CAMERA_MATRIX = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]
SQUARE_SIZE_M = 0.0254
TASK_QUEUE = "test-checkerboard-calibration"
DIVE = uuid.UUID(int=488)
TENANT = "7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11"
TARGET = CheckerboardTarget(
    rows=10, cols=14, pitch_x_m=SQUARE_SIZE_M, pitch_y_m=SQUARE_SIZE_M
)


def _capture(n: int) -> uuid.UUID:
    return uuid.UUID(int=100 + n)


def _input(*, images=2, **overrides):
    kwargs = {
        "dive_id": DIVE,
        "camera_matrix": CAMERA_MATRIX,
        "distortion_coefficients": [0.0] * 5,
        "target": TARGET,
        "images": [
            CheckerboardCalibrationImage(
                capture_id=_capture(n),
                raw=ObjectRef(
                    bucket="fishsense-lite", key=f"tenants/{TENANT}/raw/{n:032d}.ORF"
                ),
                laser_x=600.0 + n,
                laser_y=500.0,
            )
            for n in range(images)
        ],
        "dive_dots": [(600.0, 500.0)],
    }
    kwargs.update(overrides)
    return PerformCheckerboardCalibrationInput(**kwargs)


def _accepted() -> LaserCalibrationResult:
    return LaserCalibrationResult(
        outcome="accepted",
        laser_position=[0.0624, 0.0832, 0.0],
        laser_axis=[0.0, 0.0, 1.0],
        observation_count=3,
        observations_trimmed=0,
        gate_verdicts={},
        core_version="4.1.0",
    )


async def _execute(activities, payload, workflow_id):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[PerformCheckerboardCalibrationWorkflow],
            activities=activities,
        ):
            return await env.client.execute_workflow(
                PerformCheckerboardCalibrationWorkflow.run,
                payload,
                id=workflow_id,
                task_queue=TASK_QUEUE,
                result_type=LaserCalibrationResult,
            )


# ---------- the fan-out ----------


async def test_every_frame_is_dispatched_with_the_board_geometry():
    """The pitch travels in the payload, once per frame, from the dispatch.

    It is the only thing setting the scale of every length this calibration
    will later produce, so it must not be re-read anywhere downstream where a
    replay could pick up a different value than the run was started with.
    """
    seen: list[DetectCheckerboardLaserPointInput] = []

    @activity.defn(name="detect_checkerboard_laser_point")
    async def _detect(payload: DetectCheckerboardLaserPointInput):
        seen.append(payload)
        return CheckerboardObservation(
            capture_id=payload.capture_id,
            point=[0.01, 0.02, 1.4],
            laser_x=payload.laser_x,
            laser_y=payload.laser_y,
            detected_rows=10,
            detected_cols=14,
        )

    @activity.defn(name="fit_checkerboard_laser_extrinsics")
    async def _fit(_payload: FitCheckerboardExtrinsicsInput) -> LaserCalibrationResult:
        return _accepted()

    result = await _execute(
        [_detect, _fit], _input(images=3), "wf-checkerboard-geometry"
    )

    assert result == _accepted()
    # A set, not a list: the fan-out is `asyncio.gather`, so completion order
    # is not dispatch order. What must hold is that every frame is dispatched
    # exactly once, with its own dot and the dive's board geometry.
    assert sorted(p.capture_id for p in seen) == [_capture(n) for n in range(3)]
    assert {p.target for p in seen} == {TARGET}
    assert {p.capture_id: p.laser_x for p in seen} == {
        _capture(0): 600.0,
        _capture(1): 601.0,
        _capture(2): 602.0,
    }
    assert {p.capture_id: p.raw.key.rsplit("/", 1)[-1] for p in seen} == {
        _capture(n): f"{n:032d}.ORF" for n in range(3)
    }


async def test_unusable_frames_reach_the_fit_and_do_not_fail_the_dive():
    """A frame with no detectable board is ordinary, not an error.

    The fit sees every observation, including the empty ones — it is the
    single place that decides whether enough of them survived, so the count it
    reports is over the frames actually attempted. v2: the dive's dots ride
    along to the fit untouched.
    """

    @activity.defn(name="detect_checkerboard_laser_point")
    async def _detect(payload: DetectCheckerboardLaserPointInput):
        usable = payload.capture_id != _capture(1)
        return CheckerboardObservation(
            capture_id=payload.capture_id,
            point=[0.01, 0.02, 1.4] if usable else None,
            laser_x=payload.laser_x,
            laser_y=payload.laser_y,
        )

    received: list[FitCheckerboardExtrinsicsInput] = []

    @activity.defn(name="fit_checkerboard_laser_extrinsics")
    async def _fit(payload: FitCheckerboardExtrinsicsInput) -> LaserCalibrationResult:
        received.append(payload)
        return _accepted()

    await _execute([_detect, _fit], _input(images=3), "wf-checkerboard-partial")

    assert len(received) == 1
    observations = received[0].observations
    assert len(observations) == 3
    assert [o.point is None for o in observations] == [False, True, False]
    assert received[0].dive_dots == [(600.0, 500.0)]
    assert received[0].dive_id == DIVE


# ---------- the fit ----------


def _observation(n: int, point, *, laser_x=600.0, laser_y=500.0):
    return CheckerboardObservation(
        capture_id=_capture(n), point=point, laser_x=laser_x, laser_y=laser_y
    )


#: A realistic fitted origin: 10.4 cm from the camera centre, which is where
#: every sound calibration in the fleet sits.
_GOOD_ORIGIN_XY = (0.0624, 0.0832)


def _fake_calibrate_laser(_points):
    """Stand in for the Rust Atanasov kernel.

    Returns the 2-vector origin it really returns — z is implicit — so the
    padding the activity does stays under test rather than being assumed.
    """
    return np.array(_GOOD_ORIGIN_XY), np.array([0.0, 0.0, 1.0])


def _fit_input(observations):
    """The dive's own dots are built on the projection of the ray
    `_fake_calibrate_laser` returns — "the whole dive agrees", which is what
    these tests mean to describe. A fit the dive disagrees with is covered by
    `test_calibration_gates_are_wired.py`."""
    return FitCheckerboardExtrinsicsInput(
        dive_id=DIVE,
        camera_matrix=CAMERA_MATRIX,
        observations=observations,
        dive_dots=dive_dots_on_ray(
            (*_GOOD_ORIGIN_XY, 0.0), (0.0, 0.0, 1.0), CAMERA_MATRIX
        ),
    )


async def _fit(payload) -> LaserCalibrationResult:
    return await ActivityEnvironment().run(
        sut.fit_checkerboard_laser_extrinsics, payload
    )


async def test_fit_refuses_below_the_shared_threshold():
    """One threshold, shared with stage 13 rather than restated.

    A third copy that drifts from the cohort's `MIN_SLATE_LASER_POINTS` is the
    wedge this repo keeps rediscovering: cohort offers the dive, activity
    refuses it, nothing is written, and it is re-selected hourly forever.
    """
    payload = _fit_input([_observation(0, [0.0, 0.0, 1.4]), _observation(1, None)])

    result = await _fit(payload)

    assert result.outcome == "refused"
    assert result.refusal_type == "InsufficientCheckerboardPoints"
    assert "insufficient checkerboard laser points" in result.refusal_reason


async def test_fit_returns_the_extrinsics_it_computed(monkeypatch):
    monkeypatch.setattr(fit_module, "_calibrate_laser", _fake_calibrate_laser)
    monkeypatch.setattr(
        fit_module, "check_fit_self_consistency", lambda *a, **k: "passed"
    )

    # 1.2 m to 2.0 m: a real board burst, wide enough that
    # `check_observation_geometry` does not refuse it as underdetermined.
    payload = _fit_input(
        [
            _observation(0, [0.0, 0.0, 1.20]),
            _observation(1, [0.0, 0.0, 2.00], laser_x=620.0),
            _observation(2, None),
        ]
    )

    result = await _fit(payload)

    assert result.outcome == "accepted"
    # The Rust kernel returns a 2-vector origin with z implicit; stage 13 pads
    # it the same way, and the row holds a 3-vector.
    assert result.laser_position == [*_GOOD_ORIGIN_XY, 0.0]
    assert result.observation_count == 2


async def test_fit_does_not_accept_when_the_gate_rejects(monkeypatch):
    """The self-consistency gate is the last thing between a bad fit and prod.

    A mixed dot population shipped a calibration whose length errors reached
    +137% downstream on prod dive 77. The gate refuses; nothing is accepted.
    """
    from fishsense_services_processor.calibration.consistency import (
        CalibrationInconsistentError,
    )

    monkeypatch.setattr(fit_module, "_calibrate_laser", _fake_calibrate_laser)

    def _reject(*_args, **_kwargs):
        raise CalibrationInconsistentError("fit disagrees with its own dots")

    monkeypatch.setattr(fit_module, "check_fit_self_consistency", _reject)

    payload = _fit_input(
        [_observation(0, [0.0, 0.0, 1.2]), _observation(1, [0.0, 0.0, 2.0])]
    )

    result = await _fit(payload)

    assert result.outcome == "refused"
    assert "disagrees" in result.refusal_reason
    assert result.laser_position is None


async def test_an_error_that_is_not_a_refusal_still_fails_the_activity(monkeypatch):
    """Only the four gates' refusals are data. Anything else -- a bug -- must
    fail loud and retry, as in v1, rather than be recorded as a refusal that
    parks a healthy dive."""
    monkeypatch.setattr(fit_module, "_calibrate_laser", _fake_calibrate_laser)

    def _broken(*_args, **_kwargs):
        raise ValueError("a bug, not a verdict")

    monkeypatch.setattr(fit_module, "check_fit_self_consistency", _broken)

    payload = _fit_input(
        [_observation(0, [0.0, 0.0, 1.2]), _observation(1, [0.0, 0.0, 2.0])]
    )

    with pytest.raises(ValueError, match="a bug"):
        await _fit(payload)


def test_the_threshold_is_the_shared_one():
    """Shared, not restated — asserted so a later edit cannot fork it."""
    from fishsense_services_contracts import calibration_bounds

    assert fit_module.MIN_LASER_POINTS is calibration_bounds.MIN_LASER_POINTS


# ---------- the skip tally ----------
#
# A dive fitted from half its frames and one fitted from all of them look
# identical afterwards, and the first is telling you something. Same reason
# the laser-depth stage counts `skipped_invalid_geometry`.


def _skipped(n: int, reason: str) -> CheckerboardObservation:
    return CheckerboardObservation(
        capture_id=_capture(n),
        point=None,
        laser_x=1.0,
        laser_y=1.0,
        skip_reason=reason,
    )


async def test_the_fit_tallies_why_frames_were_dropped(monkeypatch, caplog):
    """The reasons reach the log, not just the count."""
    monkeypatch.setattr(fit_module, "_calibrate_laser", _fake_calibrate_laser)
    monkeypatch.setattr(
        fit_module, "check_fit_self_consistency", lambda *a, **k: "passed"
    )

    payload = _fit_input(
        [
            _observation(0, [0.0, 0.0, 1.20]),
            _observation(1, [0.0, 0.0, 2.00], laser_x=620.0),
            _skipped(2, "dot_off_board"),
            _skipped(3, "dot_off_board"),
            _skipped(4, "no_usable_board"),
        ]
    )

    with caplog.at_level(logging.INFO):
        result = await _fit(payload)

    assert result.outcome == "accepted"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "usable=2 of 5" in logged
    assert "'dot_off_board': 2" in logged
    assert "'no_usable_board': 1" in logged


async def test_the_refusal_says_why_the_frames_went():
    """A dive that falls short must not just say "not enough points".

    The remedy differs entirely by reason: every frame `dot_off_board` means
    the laser was not on the board and the capture is the problem, while
    `no_usable_board` means the board was not found and the target link or the
    frames are. Without the tally an operator sees the same message for both.
    """
    payload = _fit_input(
        [_observation(0, [0.0, 0.0, 1.4]), _skipped(1, "dot_off_board")]
    )

    result = await _fit(payload)

    assert result.outcome == "refused"
    assert "dot_off_board" in result.refusal_reason
    assert "from 2 frames" in result.refusal_reason
