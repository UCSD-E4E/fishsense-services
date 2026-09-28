"""Laser depth: what the orchestrator hands the processor, and what comes back.

v1 had no contract here: `compute_laser_depths_activity(dive_id)` read its own
inputs through the API SDK and wrote its own rows (fishsense-lite@77e8f8e5).
v2's processor never touches the database, so the orchestrator resolves the
inputs -- the camera matrix, the dive's effective laser calibration, and per
capture its valid laser labels in the order to try them -- and persists what
comes back.

v2 addition, pinned here: an image none of whose labels triangulates in front
of the camera is **refused, and the refusal comes back** (PLAN.md §9.16). v1
counted it and wrote nothing, so the dive stayed in its cohort forever (dive
32 blocked 49 dives for 23 hours).
"""

import math
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    ComputeLaserDepthsResult,
    LaserCalibrationGeometry,
    LaserDepth,
    LaserDepthCapture,
    LaserDepthOutcome,
    LaserDepthRefusal,
    LaserDot,
)

K = ((3000.0, 0.0, 2048.0), (0.0, 3000.0, 1536.0), (0.0, 0.0, 1.0))


def _geometry(**overrides) -> LaserCalibrationGeometry:
    return LaserCalibrationGeometry(
        laser_calibration_id=uuid4(),
        laser_position=overrides.get("laser_position", (-0.03, -0.10, 0.0)),
        laser_axis=overrides.get("laser_axis", (0.0, -0.02, 1.0)),
    )


def test_the_input_round_trips():
    payload = ComputeLaserDepthsInput(
        dive_id=uuid4(),
        camera_matrix=K,
        calibration=_geometry(),
        captures=[
            LaserDepthCapture(
                capture_id=uuid4(),
                laser_labels=[
                    LaserDot(laser_label_id=uuid4(), x=1900.0, y=1400.0),
                    LaserDot(laser_label_id=uuid4(), x=1901.0, y=1401.0),
                ],
            )
        ],
    )

    assert ComputeLaserDepthsInput.model_validate_json(payload.model_dump_json()) == (
        payload
    )


def test_the_label_order_is_kept():
    """The processor takes the first label that triangulates, so the order is
    the preference (v1: ascending label id)."""
    dots = [LaserDot(laser_label_id=uuid4(), x=float(i), y=0.0) for i in range(5)]
    capture = LaserDepthCapture(capture_id=uuid4(), laser_labels=dots)

    again = LaserDepthCapture.model_validate_json(capture.model_dump_json())

    assert [d.laser_label_id for d in again.laser_labels] == [
        d.laser_label_id for d in dots
    ]


def test_a_capture_with_no_label_to_try_is_refused():
    """The orchestrator sends only captures with work; an empty one is a bug
    on its side, better caught at the boundary than as a silent no-op."""
    with pytest.raises(ValidationError):
        LaserDepthCapture(capture_id=uuid4(), laser_labels=[])


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_coordinate_is_refused(bad):
    with pytest.raises(ValidationError):
        LaserDot(laser_label_id=uuid4(), x=bad, y=0.0)


def test_the_camera_matrix_is_three_by_three():
    with pytest.raises(ValidationError):
        ComputeLaserDepthsInput(
            dive_id=uuid4(),
            camera_matrix=((1.0, 0.0), (0.0, 1.0)),
            calibration=_geometry(),
            captures=[],
        )


def test_laser_vectors_are_three_vectors():
    with pytest.raises(ValidationError):
        _geometry(laser_axis=(0.0, 1.0))


def test_a_depth_must_be_in_front_of_the_camera():
    """`depth_m > 0` is the gate, not finiteness (v1's test_laser_geometry): a
    depth at or behind the camera is a refusal, never a depth."""
    for bad in (0.0, -1.2, float("nan")):
        with pytest.raises(ValidationError):
            LaserDepth(
                laser_label_id=uuid4(),
                x=1.0,
                y=2.0,
                depth_m=bad,
                range_m=1.0,
                residual_m=0.0,
            )


def test_a_depth_echoes_the_dot_it_was_computed_at():
    """Label Studio sync moves a dot in place, keeping the label id: the
    orchestrator checks the echoed dot against the label's current one before
    it writes, as it does a refusal's."""
    with pytest.raises(ValidationError):
        LaserDepth(laser_label_id=uuid4(), depth_m=1.2, range_m=1.25, residual_m=None)
    with pytest.raises(ValidationError):
        LaserDepth(
            laser_label_id=uuid4(),
            x=math.nan,
            y=2.0,
            depth_m=1.2,
            range_m=1.25,
            residual_m=None,
        )


def test_a_refusal_says_why_and_what_was_tried():
    label = uuid4()
    refusal = LaserDepthRefusal(
        laser_label_id=label,
        x=30.0,
        y=3000.0,
        reason="non_positive_depth",
        depth_m=-0.4,
    )

    again = LaserDepthRefusal.model_validate_json(refusal.model_dump_json())

    assert again == refusal
    with pytest.raises(ValidationError):
        LaserDepthRefusal(
            laser_label_id=label, x=0.0, y=0.0, reason="bored", depth_m=None
        )


def test_a_non_finite_depth_travels_as_unknown():
    """JSON has no NaN: a degenerate solve's depth is carried as None."""
    refusal = LaserDepthRefusal(
        laser_label_id=uuid4(), x=0.0, y=0.0, reason="non_finite_depth", depth_m=None
    )

    assert refusal.depth_m is None
    with pytest.raises(ValidationError):
        LaserDepthRefusal(
            laser_label_id=uuid4(),
            x=0.0,
            y=0.0,
            reason="non_finite_depth",
            depth_m=math.nan,
        )


def test_the_result_names_the_core_that_made_it():
    """laser_depths requires core_version on every row it did not migrate."""
    with pytest.raises(ValidationError):
        ComputeLaserDepthsResult(dive_id=uuid4(), core_version="", captures=[])

    result = ComputeLaserDepthsResult(
        dive_id=uuid4(),
        core_version="4.1.0",
        captures=[
            LaserDepthOutcome(
                capture_id=uuid4(),
                depth=LaserDepth(
                    laser_label_id=uuid4(),
                    x=1900.0,
                    y=1400.0,
                    depth_m=1.2,
                    range_m=1.25,
                    residual_m=None,
                ),
                refusals=[],
            )
        ],
    )
    assert ComputeLaserDepthsResult.model_validate_json(result.model_dump_json()) == (
        result
    )
