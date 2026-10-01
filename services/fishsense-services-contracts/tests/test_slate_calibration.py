"""The slate and calibration stages' contract DTOs.

Informed by fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
preprocess_contracts.py (`PreprocessSlateImagesInput`,
`CheckerboardCalibrationImage`, `PerformCheckerboardCalibrationInput`,
`CheckerboardObservation`, `VerifyCheckerboardLatticeInput`,
`CheckerboardLatticeRender`). v2 changes, each pinned here:

* ids are UUIDs, and every object the processor reads or writes is an
  ``ObjectRef`` the orchestrator issued -- never a checksum it builds a key
  from (PLAN.md §9.11);
* **stage 13 gets a contract.** v1's child took a bare dive id and its
  data-worker read and wrote through the SDK itself; v2's processor has no
  database, so the orchestrator resolves the observations and the dive's dots
  and the processor returns a `LaserCalibrationResult` for the orchestrator to
  persist -- accepted or refused, the shapes the `laser_calibrations` table
  checks;
* a checkerboard target carries its pitch per axis (PLAN.md §4.3).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import (
    SLATE_CALIBRATION_MODELS,
    CheckerboardCalibrationImage,
    CheckerboardLatticeRender,
    CheckerboardObservation,
    CheckerboardTarget,
    LaserCalibrationResult,
    LatticeImage,
    PerformCheckerboardCalibrationInput,
    PreprocessSlateImage,
    PreprocessSlateImagesInput,
    SlateCalibrationInput,
    SlateObservation,
    VerifyCheckerboardLatticeInput,
)

K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
TENANT = "7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11"


def _ref(key: str, bucket: str = "fishsense-lite") -> ObjectRef:
    return ObjectRef(bucket=bucket, key=f"tenants/{TENANT}/{key}")


def _round_trip(model):
    return type(model).model_validate_json(model.model_dump_json())


def test_stage_9_hands_the_processor_refs_not_checksums():
    payload = PreprocessSlateImagesInput(
        dive_id=uuid4(),
        slate_template_id=uuid4(),
        slate_pdf=_ref(f"slate_pdf/{uuid4()}.pdf"),
        slate_dpi=300,
        reference_points=[(10.0, 20.0), (30.0, 40.0)],
        camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        images=[
            PreprocessSlateImage(
                capture_id=uuid4(),
                raw=_ref("raw/0123abcd.ORF"),
                jpeg=_ref("preprocess_slate_images_jpeg/0123abcd.JPG", "labels"),
            )
        ],
    )

    assert _round_trip(payload) == payload


def test_a_slate_needs_a_positive_dpi():
    """The template points are in PDF pixels at this DPI; zero would divide."""
    with pytest.raises(ValidationError):
        SlateCalibrationInput(
            dive_id=uuid4(),
            camera_matrix=K,
            template_points=[(0.0, 0.0)],
            dpi=0,
            observations=[],
            dive_dots=[],
        )


def test_stage_13_carries_every_observation_and_the_dives_dots():
    """v1's activity read these itself; now they cross the wire. A label whose
    reference points are JSON null (prod dive 526) must still be carried, so
    the processor can skip the label, not the dive."""
    payload = SlateCalibrationInput(
        dive_id=uuid4(),
        camera_matrix=K,
        template_points=[(0.0, 0.0), (2400.0, 0.0)],
        dpi=300,
        observations=[
            SlateObservation(
                capture_id=uuid4(),
                reference_points=[(1.0, 2.0), (3.0, 4.0)],
                skipped_points=[1],
                laser_x=100.0,
                laser_y=200.0,
            ),
            SlateObservation(
                capture_id=uuid4(),
                reference_points=None,
                skipped_points=None,
                laser_x=100.0,
                laser_y=200.0,
            ),
        ],
        dive_dots=[(100.0, 200.0), (110.0, 230.0)],
    )

    assert _round_trip(payload) == payload


def _accepted(**overrides) -> LaserCalibrationResult:
    values = {
        "outcome": "accepted",
        "laser_position": [0.06, 0.08, 0.0],
        "laser_axis": [0.0, 0.0, 1.0],
        "observation_count": 6,
        "observations_trimmed": 0,
        "lever_arm_m": 1.2,
        "gate_verdicts": {"observation_geometry": "passed"},
        "core_version": "4.1.0",
    }
    values.update(overrides)
    return LaserCalibrationResult(**values)


def test_an_accepted_result_round_trips():
    result = _accepted()
    assert _round_trip(result) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"laser_position": None},
        {"laser_axis": [0.0, 1.0]},
        {"laser_position": [0.06, 0.08]},
    ],
)
def test_an_accepted_result_needs_both_three_vectors(overrides):
    """What `laser_calibrations_accepted_geometry_check` refuses; refused
    here first, so a malformed result fails in the processor's own run."""
    with pytest.raises(ValidationError):
        _accepted(**overrides)


def test_a_refusal_needs_its_type_and_reason():
    """The type is how the parent re-raises it (v1's ApplicationError type);
    the reason is the operator's only message."""
    with pytest.raises(ValidationError):
        LaserCalibrationResult(
            outcome="refused",
            refusal_reason="insufficient laser points (1 < 2)",
            observation_count=1,
            observations_trimmed=0,
            gate_verdicts={},
            core_version="4.1.0",
        )
    refused = LaserCalibrationResult(
        outcome="refused",
        refusal_type="InsufficientLaserPoints",
        refusal_reason="insufficient laser points (1 < 2)",
        observation_count=1,
        observations_trimmed=0,
        gate_verdicts={},
        core_version="4.1.0",
    )
    assert _round_trip(refused) == refused


def test_a_checkerboard_target_has_a_pitch_per_axis():
    target = CheckerboardTarget(rows=10, cols=14, pitch_x_m=0.04223, pitch_y_m=0.04211)
    assert _round_trip(target) == target
    with pytest.raises(ValidationError):
        CheckerboardTarget(rows=10, cols=14, pitch_x_m=0.0, pitch_y_m=0.04211)


def test_checkerboard_calibration_round_trips():
    payload = PerformCheckerboardCalibrationInput(
        dive_id=uuid4(),
        camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        target=CheckerboardTarget(rows=10, cols=14, pitch_x_m=0.042, pitch_y_m=0.042),
        images=[
            CheckerboardCalibrationImage(
                capture_id=uuid4(), raw=_ref("raw/ab12.ORF"), laser_x=1.0, laser_y=2.0
            )
        ],
        dive_dots=[(1.0, 2.0)],
    )
    assert _round_trip(payload) == payload

    skipped = CheckerboardObservation(
        capture_id=uuid4(),
        point=None,
        laser_x=1.0,
        laser_y=2.0,
        skip_reason="dot_off_board",
    )
    assert _round_trip(skipped) == skipped


def test_lattice_verification_round_trips():
    image = LatticeImage(
        capture_id=uuid4(),
        raw=_ref("raw/ab12.ORF"),
        render=_ref("checkerboard_lattice_jpeg/ab12.JPG", "labels"),
        laser_x=1.0,
        laser_y=2.0,
    )
    payload = VerifyCheckerboardLatticeInput(
        dive_id=uuid4(),
        camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        target=CheckerboardTarget(rows=10, cols=14, pitch_x_m=0.042, pitch_y_m=0.042),
        images=[image],
    )
    assert payload.sample_limit is None, "v1's default: uncapped (the parent caps)"
    assert _round_trip(payload) == payload

    render = CheckerboardLatticeRender(
        capture_id=image.capture_id,
        image=image.render,
        detected_rows=10,
        detected_cols=14,
        median_spacing_px=32.0,
        corners=[[1.0, 2.0]],
        width=4000,
        height=3000,
    )
    assert _round_trip(render) == render


def test_every_model_is_listed_for_the_published_schema():
    """Integration adds these to `MODELS` when it publishes the next
    contract version; a model missing here would cross the wire unpinned."""
    listed = set(SLATE_CALIBRATION_MODELS)
    assert {
        PreprocessSlateImage,
        PreprocessSlateImagesInput,
        SlateObservation,
        SlateCalibrationInput,
        LaserCalibrationResult,
        CheckerboardTarget,
        CheckerboardCalibrationImage,
        PerformCheckerboardCalibrationInput,
        CheckerboardObservation,
        LatticeImage,
        VerifyCheckerboardLatticeInput,
        CheckerboardLatticeRender,
    } == listed
