"""The phone measurement's payloads (v28): a phone capture's stored files,
measured on the server as FishSense Mobile measures it (#475)."""

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.masks import MASK_SOURCES, MaskUpload, mask_key
from fishsense_services_contracts.phone_measurement import (
    PHONE_ALGORITHM,
    PhoneFrame,
    PhoneMeasured,
    PhoneMeasureInput,
)

K = [1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0]
SHA = "a" * 64


def _frame(**kw):
    fields = dict(capture_id=uuid.uuid4(), rgb_url="https://s3/rgb",
                  depth_url="https://s3/d", depth_width=256, depth_height=192,
                  depth_row_bytes=1024, intrinsics=K)  # fmt: skip
    return PhoneFrame(**{**fields, **kw})


def test_a_frame_carries_its_files_its_depth_geometry_and_its_k():
    capture = uuid.uuid4()
    upload = MaskUpload(
        key=mask_key("phone", capture, uuid.uuid4()), put_url="https://s3/m"
    )
    r = PhoneMeasureInput(
        tenant_id=uuid.uuid4(), frames=[_frame(capture_id=capture, mask=upload)]
    )
    back = PhoneMeasureInput.model_validate(r.model_dump(mode="json"))
    assert back.frames[0].depth_row_bytes == 1024
    assert back.frames[0].intrinsics == K
    assert back.frames[0].mask.key.startswith(f"masks/phone/{capture}/")


def test_phone_masks_are_their_own_source():
    assert "phone" in MASK_SOURCES


def test_k_is_nine_numbers():
    with pytest.raises(ValidationError):
        _frame(intrinsics=K[:8])


def test_a_stride_narrower_than_a_row_of_depth_is_refused():
    with pytest.raises(ValidationError):
        _frame(depth_row_bytes=256 * 4 - 4)


def test_a_measured_result_names_its_length_points_and_provenance():
    r = PhoneMeasured(capture_id=uuid.uuid4(), status="measured", length_m=0.284,
                      snout_x=530.0, snout_y=690.0, fork_x=1500.0, fork_y=740.0,
                      snout_depth_m=0.38, fork_depth_m=0.44,
                      model_id="fishial/1@f721445f37db", core_version="4.3.2",
                      algorithm=PHONE_ALGORITHM)  # fmt: skip
    back = PhoneMeasured.model_validate(r.model_dump(mode="json"))
    assert back.length_m == 0.284 and back.algorithm.startswith("fishsense-mobile@")


def test_measured_without_a_length_or_points_is_refused():
    with pytest.raises(ValidationError):
        PhoneMeasured(capture_id=uuid.uuid4(), status="measured", length_m=0.3)


def test_only_a_frame_with_a_mask_may_name_a_stored_one():
    stored = {"key": mask_key("phone", uuid.uuid4(), uuid.uuid4()), "size": 10,
              "sha256": SHA, "width": 1920, "height": 1440}  # fmt: skip
    PhoneMeasured(capture_id=uuid.uuid4(), status="depth_failed",
                  snout_x=1.0, snout_y=2.0, fork_x=3.0, fork_y=4.0, mask=stored)  # fmt: skip
    with pytest.raises(ValidationError):
        PhoneMeasured(capture_id=uuid.uuid4(), status="no_fish", mask=stored)
