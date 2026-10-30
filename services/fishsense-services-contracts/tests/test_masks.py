"""A prediction's SAM mask, stored as a file in the tenant's bucket (v12).

New in v2 (2026-10-21). The mobile app syncs its fish mask as ``mask_u8``:
uint8, width x height, nonzero = fish, in the pixels of the frame it measured
(docs/handoff/fishsense-mobile/SYNC.md). Ours are stored the same way, at the
frame SAM segmented, as the PNG the app's mask is moving to (#230: 8-bit
grayscale, lossless, nonzero = fish). Each mask is a new key: a
re-run never overwrites a mask an older prediction names.
"""

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.automatic_results import (
    AutomaticFrame,
    AutomaticFrameResult,
)
from fishsense_services_contracts.headtail import (
    HeadtailPredictionResult,
    PredictHeadtailImage,
)
from fishsense_services_contracts.masks import (
    MASK_CONTENT_TYPE,
    MASK_SOURCES,
    MaskUpload,
    StoredMask,
    is_mask_of,
    mask_key,
)
from fishsense_services_contracts.object_store import ObjectRef

SHA = "a" * 64
ID = uuid.UUID(int=7)
REF = ObjectRef(bucket="b", key="k")


CAPTURE = uuid.UUID(int=3)


def test_a_mask_key_is_new_per_mask_under_its_source_and_capture():
    assert mask_key("automatic", CAPTURE, ID) == (f"masks/automatic/{CAPTURE}/{ID}.png")
    assert set(MASK_SOURCES) == {"automatic", "headtail"}
    assert MASK_CONTENT_TYPE == "image/png"


def test_a_key_says_whose_mask_it_is():
    key = mask_key("headtail", CAPTURE, ID)
    assert is_mask_of(key, "headtail", CAPTURE)
    assert not is_mask_of(key, "automatic", CAPTURE)
    assert not is_mask_of(key, "headtail", uuid.UUID(int=4))


@pytest.mark.parametrize(
    "args",
    [("other", CAPTURE, ID), ("automatic", "abc", ID),
     ("automatic", CAPTURE, "not-a-uuid")],
)  # fmt: skip
def test_a_mask_key_refuses_what_is_not_a_source_checksum_and_id(args):
    with pytest.raises(ValueError):
        mask_key(*args)


def _stored(**over):
    return StoredMask(**{"key": mask_key("automatic", CAPTURE, ID), "size": 900,
                         "sha256": SHA, "width": 4014, "height": 3016, **over})  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [{"size": 0}, {"sha256": "A" * 64}, {"sha256": "a" * 63}, {"width": 0},
     {"height": -1}, {"key": "renders/x.JPG"}],
)  # fmt: skip
def test_a_stored_mask_names_its_size_hash_and_frame(bad):
    with pytest.raises(ValidationError):
        _stored(**bad)


def test_an_upload_is_a_mask_key_and_its_put():
    MaskUpload(key=mask_key("headtail", CAPTURE, ID), put_url="https://garage/x")
    with pytest.raises(ValidationError):
        MaskUpload(key="raws/abc.ORF", put_url="https://garage/x")


def test_frames_may_carry_a_mask_upload_and_older_ones_parse_without():
    upload = MaskUpload(key=mask_key("automatic", CAPTURE, ID), put_url="u")
    assert AutomaticFrame(capture_id=ID, raw=REF, mask=upload).mask == upload
    assert AutomaticFrame(capture_id=ID, raw=REF).mask is None
    image = PredictHeadtailImage(capture_id=ID, jpeg=REF, laser_points=[[1, 2]],
                                 laser_label_ids=[ID], mask=upload)  # fmt: skip
    assert image.mask == upload


@pytest.mark.parametrize("status", ["predicted", "headtail_failed"])
def test_a_result_that_kept_a_mask_may_name_it(status):
    automatic = AutomaticFrameResult(capture_id=ID, status=status,
                                     predictor_version=3, mask=_stored())  # fmt: skip
    human = HeadtailPredictionResult(capture_id=ID, status=status, mask=_stored())
    assert automatic.mask.size == human.mask.size == 900


@pytest.mark.parametrize("status", ["no_detections", "laser_off_all_fish",
                                    "mask_unscored", "slate_frame"])  # fmt: skip
def test_a_result_with_no_kept_mask_names_none(status):
    with pytest.raises(ValidationError):
        AutomaticFrameResult(capture_id=ID, status=status, predictor_version=3,
                             mask=_stored())  # fmt: skip
    if status in ("no_detections", "laser_off_all_fish"):
        with pytest.raises(ValidationError):
            HeadtailPredictionResult(capture_id=ID, status=status, mask=_stored())
