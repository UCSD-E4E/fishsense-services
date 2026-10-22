"""The mask backfill's payloads (v14): an existing prediction's mask,
regenerated and stored only when it reproduces that prediction."""

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.mask_backfill import (
    MASK_AREA_TOLERANCE,
    MASK_POINT_TOLERANCE_PX,
    BackfilledMask,
    BackfillMaskFrame,
    MaskExpectation,
    reproduces,
)
from fishsense_services_contracts.masks import StoredMask, mask_key
from fishsense_services_contracts.object_store import ObjectRef

EXPECT = MaskExpectation(mask_area_px=10_000, crop_x=100, crop_y=200,
                         head_x=10.0, head_y=20.0, tail_x=300.0, tail_y=40.0)  # fmt: skip


def _frame(**kw):
    return BackfillMaskFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                             dots=[[1.0, 2.0]], expect=EXPECT, **kw)  # fmt: skip


def test_a_frame_names_exactly_one_image():
    _frame(render_url="https://g/r?sig")
    _frame(jpeg=ObjectRef(bucket="fishsense-tenant-lab", key="jpegs/x.JPG"))
    with pytest.raises(ValidationError):
        _frame()
    with pytest.raises(ValidationError):
        _frame(render_url="u", jpeg=ObjectRef(bucket="fishsense-tenant-lab", key="k"))


def test_the_same_mask_reproduces_and_a_different_one_does_not():
    assert reproduces(EXPECT, EXPECT)
    near = EXPECT.model_copy(update={
        "mask_area_px": int(10_000 * (1 + MASK_AREA_TOLERANCE)),
        "head_x": 10.0 + MASK_POINT_TOLERANCE_PX})  # fmt: skip
    assert reproduces(EXPECT, near)
    for change in ({"mask_area_px": 10_100}, {"crop_x": 101},
                   {"head_x": 10.0 + 2 * MASK_POINT_TOLERANCE_PX}, {"tail_y": None}):  # fmt: skip
        assert not reproduces(EXPECT, EXPECT.model_copy(update=change)), change


def test_only_a_reproduced_mask_is_named():
    p, c = uuid.uuid4(), uuid.uuid4()
    stored = StoredMask(key=mask_key("automatic", c, uuid.uuid4()), size=10,
                        sha256="0" * 64, width=4014, height=3016)  # fmt: skip
    BackfilledMask(prediction_id=p, capture_id=c, outcome="reproduced", got=EXPECT,
                   mask=stored)  # fmt: skip
    with pytest.raises(ValidationError):
        BackfilledMask(prediction_id=p, capture_id=c, outcome="mismatch", got=EXPECT,
                       mask=stored)  # fmt: skip
    with pytest.raises(ValidationError):
        BackfilledMask(prediction_id=p, capture_id=c, outcome="nonsense")
