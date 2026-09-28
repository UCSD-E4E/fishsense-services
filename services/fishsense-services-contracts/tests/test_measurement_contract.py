"""Stage 14 (measure fish): what the orchestrator hands the processor, and back.

v1 had no contract here: `measure_fish_activity(dive_id)` read its own inputs
and wrote its own rows (fishsense-lite@77e8f8e5). In v2 the orchestrator
resolves, per capture, the one laser label and the one head/tail label to
measure from; the processor returns a length or a refusal, echoing the inputs
so the refusal can be recorded against exactly what was tried.

v2 additions, pinned here:

* **a zero or non-finite length is a refusal, and it comes back** (PLAN.md
  §9.16). v1 dropped a NaN and moved on, and could write a zero length that v2's
  `measurements` (CHECK length_m > 0) would reject: either way, the dive stayed
  in its cohort;
* **the result names its algorithm, version and core** -- 0013 requires them
  of every server measurement.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.laser_depth import LaserCalibrationGeometry, LaserDot
from fishsense_services_contracts.measurement import (
    FishLength,
    HeadTail,
    MeasureFishCapture,
    MeasureFishInput,
    MeasureFishResult,
)

K = ((3000.0, 0.0, 2048.0), (0.0, 3000.0, 1536.0), (0.0, 0.0, 1.0))


def _dot():
    return LaserDot(laser_label_id=uuid4(), x=1900.0, y=1400.0)


def _head_tail():
    return HeadTail(
        head_tail_label_id=uuid4(),
        head_x=1800.0,
        head_y=1500.0,
        tail_x=2100.0,
        tail_y=1500.0,
    )


def _capture():
    return MeasureFishCapture(
        capture_id=uuid4(),
        species_label_id=uuid4(),
        laser=_dot(),
        head_tail=_head_tail(),
    )


def test_the_input_round_trips():
    payload = MeasureFishInput(
        dive_id=uuid4(),
        camera_matrix=K,
        calibration=LaserCalibrationGeometry(
            laser_calibration_id=uuid4(),
            laser_position=(-0.03, -0.10, 0.0),
            laser_axis=(0.0, -0.02, 1.0),
        ),
        captures=[_capture(), _capture()],
    )

    assert MeasureFishInput.model_validate_json(payload.model_dump_json()) == payload


def test_every_keypoint_is_required():
    """v1's `_has_complete_keypoints`: the orchestrator sends only valid
    head/tail labels, so a missing coordinate is refused at the boundary."""
    with pytest.raises(ValidationError):
        HeadTail(
            head_tail_label_id=uuid4(),
            head_x=1.0,
            head_y=None,
            tail_x=3.0,
            tail_y=4.0,
        )


def _length(**overrides):
    capture = _capture()
    fields = dict(
        capture_id=capture.capture_id,
        species_label_id=capture.species_label_id,
        laser=capture.laser,
        head_tail=capture.head_tail,
        length_m=0.3,
        depth_m=1.2,
        refusal=None,
    )
    fields.update(overrides)
    return FishLength(**fields)


def test_a_length_is_positive_and_finite():
    assert _length(length_m=0.3).length_m == 0.3
    for bad in (0.0, -0.3, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            _length(length_m=bad)


def test_a_result_is_a_length_or_a_refusal_never_both_or_neither():
    _length(length_m=None, refusal="zero_length")
    _length(length_m=None, depth_m=None, refusal="non_finite_length")
    with pytest.raises(ValidationError):
        _length(length_m=0.3, refusal="zero_length")
    with pytest.raises(ValidationError):
        _length(length_m=None, refusal=None)


def test_the_result_names_what_made_it():
    for missing in ("algorithm", "algorithm_version", "core_version"):
        fields = dict(
            dive_id=uuid4(),
            algorithm="laser_depth_fronto_parallel",
            algorithm_version="1",
            core_version="4.1.0",
            captures=[],
        )
        fields[missing] = ""
        with pytest.raises(ValidationError):
            MeasureFishResult(**fields)

    result = MeasureFishResult(
        dive_id=uuid4(),
        algorithm="laser_depth_fronto_parallel",
        algorithm_version="1",
        core_version="4.1.0",
        captures=[_length()],
    )
    assert MeasureFishResult.model_validate_json(result.model_dump_json()) == result
