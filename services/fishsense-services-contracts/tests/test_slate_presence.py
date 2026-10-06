"""The slate detector's contract: is a dive slate anywhere in this frame?

New in v2: v1's slate predictor estimated a slate's *pose* and was retired
(2026-08-03); this one answers presence only. The model is
2026-10-03_slate_detector@95a77d95's EfficientNet-B0 with GeM pooling
(runs/final-q1). Pinned here because both sides read the constants: the
processor stamps the version, the orchestrator's cohort selects on a mismatch
with it, and the threshold is the model's operating point (precision 0.999,
recall 0.993 at 0.5 in 5-fold CV grouped by dive).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts import MODELS
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_presence import (
    SLATE_DETECTOR_MODEL_NAME,
    SLATE_DETECTOR_VERSION,
    SLATE_INPUT_HEIGHT,
    SLATE_INPUT_WIDTH,
    SLATE_PRESENCE_STATUSES,
    SLATE_PRESENCE_THRESHOLD,
    DetectSlateImage,
    DetectSlateImageInput,
    DetectSlateImagesInput,
    SlatePresenceResult,
    SlateRender,
    is_slate,
)

RAW = ObjectRef(bucket="scratch", key=f"tenants/{uuid4()}/raw/abc.ORF")
K = [[3500.0, 0.0, 2000.0], [0.0, 3500.0, 1500.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]
SHA = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"


AT = datetime(2026, 10, 5, 12, tzinfo=UTC)


def _render(**overrides) -> SlateRender:
    values = {
        "decode_config": "production",
        "decode_params": {"stretch_mode": "off", "clahe_enabled": True},
    }
    return SlateRender(**{**values, **overrides})


def _result(**overrides) -> SlatePresenceResult:
    values = {
        "capture_id": uuid4(),
        "status": "predicted",
        "probability": 0.97,
        "model_version": SLATE_DETECTOR_VERSION,
        "weights_sha256": SHA,
        "core_version": "4.1.0",
        "processor_version": "0.1.2",
        "render": _render(),
        "predicted_at": AT,
    }
    return SlatePresenceResult(**{**values, **overrides})


def test_a_result_records_enough_to_reproduce_it():
    """For publication: the model and its weights, fishsense-core and the
    processor that ran it, how the frame was decoded and sized, and when."""
    result = _result()

    assert result.model_name == SLATE_DETECTOR_MODEL_NAME == "slate-detector"
    assert (result.core_version, result.processor_version) == ("4.1.0", "0.1.2")
    assert result.predicted_at == AT
    render = result.render
    assert (render.decode_config, render.rectified) == ("production", True)
    assert (
        (render.input_width, render.input_height)
        == (
            SLATE_INPUT_WIDTH,
            SLATE_INPUT_HEIGHT,
        )
        == (1024, 768)
    )
    assert (render.cache_long_side, render.jpeg_quality, render.tta) == (
        1600,
        95,
        "hflip",
    )


def test_a_naive_timestamp_is_refused():
    with pytest.raises(ValidationError):
        _result(predicted_at=datetime(2026, 10, 5, 12))


def test_the_constants():
    """Version 1 is runs/final-q1; 0.5 is the operating point the CV numbers
    were measured at."""
    assert SLATE_DETECTOR_VERSION == 1
    assert SLATE_PRESENCE_THRESHOLD == 0.5
    assert SLATE_PRESENCE_STATUSES == ("predicted", "decode_failed")


@pytest.mark.parametrize(
    ("probability", "expected"), [(0.5, True), (0.4999, False), (0.99, True)]
)
def test_slate_is_at_or_above_the_threshold(probability, expected):
    assert is_slate(probability) is expected


def test_the_models_are_contract():
    assert {
        DetectSlateImage,
        DetectSlateImageInput,
        DetectSlateImagesInput,
        SlatePresenceResult,
        SlateRender,
    } <= set(MODELS)


def test_the_workflow_input_round_trips():
    payload = DetectSlateImagesInput(
        tenant_id=uuid4(),
        dive_id=uuid4(),
        camera_matrix=K,
        distortion_coefficients=D,
        images=[DetectSlateImage(capture_id=uuid4(), raw=RAW)],
    )

    assert DetectSlateImagesInput.model_validate_json(payload.model_dump_json()) == (
        payload
    )


@pytest.mark.parametrize("matrix", [[[1.0, 0.0], [0.0, 1.0]], K[:2], [*K, [0, 0, 1]]])
def test_the_camera_matrix_is_3x3(matrix):
    with pytest.raises(ValidationError):
        DetectSlateImageInput(
            image=DetectSlateImage(capture_id=uuid4(), raw=RAW),
            camera_matrix=matrix,
            distortion_coefficients=D,
        )


def test_a_prediction_carries_its_probability_and_provenance():
    result = _result()

    assert (result.status, result.probability, result.model_version) == (
        "predicted",
        0.97,
        SLATE_DETECTOR_VERSION,
    )
    assert result.weights_sha256 == SHA


def test_a_decode_failure_carries_no_probability():
    """An abstention is recorded too: the cohort selects on a row's absence."""
    assert _result(status="decode_failed", probability=None).probability is None
    with pytest.raises(ValidationError):
        _result(status="decode_failed", probability=0.2)


def test_a_prediction_needs_its_probability():
    with pytest.raises(ValidationError):
        _result(probability=None)


@pytest.mark.parametrize("probability", [-0.01, 1.01])
def test_a_probability_is_one(probability):
    with pytest.raises(ValidationError):
        _result(probability=probability)


def test_an_unknown_status_is_refused():
    with pytest.raises(ValidationError):
        _result(status="skipped")


@pytest.mark.parametrize("sha", ["abc", SHA.upper()[:-1] + "Z", SHA + "0"])
def test_the_weights_are_named_by_their_sha256(sha):
    with pytest.raises(ValidationError):
        _result(weights_sha256=sha)


def test_the_sha256_is_normalised_to_lower_case():
    assert _result(weights_sha256=SHA.upper()).weights_sha256 == SHA
