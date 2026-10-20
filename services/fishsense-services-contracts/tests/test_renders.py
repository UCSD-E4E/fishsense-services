"""Every capture's render: the raw rectified to a JPEG, once, in the tenant's
own bucket, read by every step after it (2026-10-19)."""

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_JPEG_RENDER_VERSION,
    AutomaticCalibrationFrame,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.renders import (
    RENDER_STATUSES,
    RENDER_VERSION,
    RenderCaptureInput,
    RenderFrame,
    RenderResult,
    render_key,
)
from fishsense_services_contracts.species_prediction import PredictSpeciesImage

CAMERA = uuid.UUID("0123456789abcdef0123456789abcdef")
SUM = "c" * 32


def test_the_key_names_the_render_version_the_camera_and_the_raw():
    assert render_key(1, CAMERA, SUM) == f"renders/r1_{CAMERA.hex}/{SUM}.JPG"


@pytest.mark.parametrize(
    "version, camera, checksum",
    [(0, CAMERA, SUM), (True, CAMERA, SUM), (1, str(CAMERA), SUM),
     (1, CAMERA, "../x"), (1, CAMERA, "")],
)  # fmt: skip
def test_a_key_needs_a_version_a_camera_and_a_checksum(version, camera, checksum):
    with pytest.raises(ValueError):
        render_key(version, camera, checksum)


def test_the_automatic_track_reads_the_one_render():
    assert AUTOMATIC_JPEG_RENDER_VERSION == RENDER_VERSION


def _frame():
    return RenderFrame(capture_id=uuid.uuid4(),
                       raw=ObjectRef(bucket="scratch", key=f"raw/{SUM}.ORF"),
                       key=render_key(RENDER_VERSION, CAMERA, SUM),
                       put_url="https://s3.example.com/b/k?X-Amz-Signature=x")  # fmt: skip


def test_a_render_input_carries_the_camera_and_where_to_put_it():
    payload = RenderCaptureInput(
        frame=_frame(), render_version=RENDER_VERSION, camera_calibration_id=CAMERA,
        camera_matrix=[[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]],
        distortion_coefficients=[0.0] * 5,
    )  # fmt: skip
    assert payload.frame.key.startswith("renders/")


def test_a_rendered_result_names_its_bytes():
    ok = RenderResult(capture_id=uuid.uuid4(), status="rendered",
                      render_version=1, camera_calibration_id=CAMERA,
                      bytes=1234, md5="0" * 32)  # fmt: skip
    assert ok.md5 == "0" * 32
    with pytest.raises(ValidationError):
        RenderResult(capture_id=uuid.uuid4(), status="rendered", render_version=1,
                     camera_calibration_id=CAMERA)  # fmt: skip
    with pytest.raises(ValidationError):
        RenderResult(capture_id=uuid.uuid4(), status="rendered", render_version=1,
                     camera_calibration_id=CAMERA, bytes=1, md5="xyz")  # fmt: skip
    with pytest.raises(ValidationError):
        RenderResult(capture_id=uuid.uuid4(), status="gone", render_version=1,
                     camera_calibration_id=CAMERA)  # fmt: skip


def test_an_abstention_has_no_bytes():
    assert set(RENDER_STATUSES) == {"rendered", "raw_unavailable", "decode_failed"}
    r = RenderResult(capture_id=uuid.uuid4(), status="raw_unavailable",
                     render_version=1, camera_calibration_id=CAMERA)  # fmt: skip
    assert r.bytes is None and r.md5 is None


@pytest.mark.parametrize("model, extra", [
    (PredictSpeciesImage, dict(headtail_prediction_id=uuid.uuid4(), mask_bbox=[1, 2, 3, 4])),
    (AutomaticCalibrationFrame, dict(x=1.0, y=2.0)),
])  # fmt: skip
def test_a_reader_takes_the_render_by_url_or_a_jpeg_by_ref_but_one(model, extra):
    url = "https://s3.example.com/t/renders/k.JPG?X-Amz-Signature=x"
    ref = ObjectRef(bucket="b", key=f"preprocess_headtail_jpeg/{SUM}.JPG")
    assert model(capture_id=uuid.uuid4(), jpeg_url=url, **extra).jpeg_url == url
    assert model(capture_id=uuid.uuid4(), jpeg=ref, **extra).jpeg == ref
    with pytest.raises(ValidationError):
        model(capture_id=uuid.uuid4(), **extra)
    with pytest.raises(ValidationError):
        model(capture_id=uuid.uuid4(), jpeg=ref, jpeg_url=url, **extra)
