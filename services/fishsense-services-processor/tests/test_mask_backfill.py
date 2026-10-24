"""The mask backfill (contract 14): an existing prediction's mask, regenerated
by the kernel that made it, stored only when it reproduces the prediction.

Runs the real kernels (`automatic_frame_result`, `predict_from_jpeg`) with
stub segmenters, as the frames stage's tests do: the first run records the
prediction, the second must reproduce it.
"""

from __future__ import annotations

import hashlib
import uuid

import cv2
import numpy as np
import pytest
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.mask_backfill import (
    BackfillMaskFrame,
    MaskExpectation,
)
from fishsense_services_contracts.masks import MaskUpload, mask_key
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_core.fish.fish_at_dot import crop_origin
from fishsense_services_processor.mask_backfill.activities import (
    MaskBackfillActivities,
)
from fishsense_services_processor.mask_backfill.regenerate import regenerate

FRAME_W, FRAME_H = 4014, 3016
CROP_W, CROP_H = 1800, 1350
DOT = (2000.0, 1500.0)
CAPTURE = uuid.uuid4()


def _jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((FRAME_H, FRAME_W, 3), 40, np.uint8))
    assert ok
    return buf.tobytes()


def _mask(dx=0, half_len=200, half_h=60):
    ox, oy = crop_origin(*DOT, FRAME_W, FRAME_H, CROP_W, CROP_H)
    m = np.zeros((CROP_H, CROP_W), np.uint8)
    cv2.ellipse(m, (int(DOT[0] + dx - ox), int(DOT[1] - oy)), (half_len, half_h),
                0, 0, 360, 1, -1)  # fmt: skip
    return m


class _Scored:
    def __init__(self, pairs):
        self.pairs = pairs

    def segment(self, image):
        # core's ScoredSegmenter: the plain seam, the same masks
        return [mask for mask, _ in self.segment_scored(image)]

    def segment_scored(self, image):
        return self.pairs


class _Plain:
    def __init__(self, masks):
        self.masks = masks

    def segment(self, image):
        return self.masks


def _first(source, jpeg, seg) -> MaskExpectation:
    """What the original prediction recorded: the same kernel's first run."""
    frame = BackfillMaskFrame(
        prediction_id=uuid.uuid4(), capture_id=CAPTURE, dots=[list(DOT)],
        render_url="u", expect=MaskExpectation(mask_area_px=1, crop_x=0, crop_y=0),
    )  # fmt: skip
    _, got, _ = regenerate(source, frame, jpeg, scored=seg, plain=seg,
                           checkpoint="sam3/3.1@abc", core_version="4.1.0")  # fmt: skip
    return got


def _frame(expect, **kw):
    kw.setdefault("render_url", "u")
    return BackfillMaskFrame(prediction_id=uuid.uuid4(), capture_id=CAPTURE,
                             dots=[list(DOT)], expect=expect, **kw)  # fmt: skip


class _Both(_Scored, _Plain):
    def __init__(self, mask, score=0.9):
        _Scored.__init__(self, [(mask, score)])
        _Plain.__init__(self, [mask])


@pytest.mark.parametrize("source", ["automatic", "headtail"])
def test_the_same_mask_reproduces_its_prediction_and_is_kept(source):
    jpeg, seg = _jpeg(), _Both(_mask())
    expect = _first(source, jpeg, seg)
    outcome, got, mask = regenerate(source, _frame(expect), jpeg, scored=seg, plain=seg,
                                    checkpoint="sam3/3.1@abc", core_version="4.1.0")  # fmt: skip
    assert outcome == "reproduced" and got == expect
    assert (
        mask is not None
        and int((np.asarray(mask.crop) > 0).sum()) == expect.mask_area_px
    )


@pytest.mark.parametrize("source", ["automatic", "headtail"])
def test_a_different_mask_is_not_the_predictions(source):
    jpeg = _jpeg()
    expect = _first(source, jpeg, _Both(_mask()))
    other = _Both(_mask(half_len=260))
    outcome, got, mask = regenerate(source, _frame(expect), jpeg, scored=other,
                                    plain=other, checkpoint="c", core_version="v")  # fmt: skip
    assert outcome == "mismatch" and got != expect and mask is None


def test_no_mask_under_the_dot_now_is_said():
    jpeg = _jpeg()
    expect = _first("automatic", jpeg, _Both(_mask()))
    off = _Both(_mask(dx=700))
    outcome, got, mask = regenerate("automatic", _frame(expect), jpeg, scored=off,
                                    plain=off, checkpoint="c", core_version="v")  # fmt: skip
    assert (outcome, got, mask) == ("no_mask", None, None)


def test_an_image_that_does_not_decode_is_said():
    expect = MaskExpectation(mask_area_px=10, crop_x=0, crop_y=0)
    seg = _Both(_mask())
    outcome, _, _ = regenerate("headtail", _frame(expect), b"not a jpeg", scored=seg,
                               plain=seg, checkpoint="c", core_version="v")  # fmt: skip
    assert outcome == "decode_failed"


# -- the activity ----------------------------------------------------------------


class _Store:
    def __init__(self, data):
        self.data = data
        self.asked = []

    async def download_image(self, ref=None, url=None):
        self.asked.append(url or ref)
        return self.data

    async def download_processed_jpeg(self, ref):
        self.asked.append(ref)
        return self.data


def _activities(store, *, gpu=True, seg=None, puts=None):
    seg = seg or _Both(_mask())

    async def checkpoint():
        return "/w/sam3.1.pt", "sam3/3.1@abc"

    def put(url, headers, data):
        if puts is not None:
            puts.append((url, data))
        return 200

    return MaskBackfillActivities(
        store_factory=lambda: store, sam3_checkpoint=checkpoint,
        cuda_available=lambda: gpu, scored_segmenter=lambda path: seg,
        plain_segmenter=lambda path: seg, put=put,
    )  # fmt: skip


async def test_a_reproduced_mask_is_put_and_named():
    jpeg, puts = _jpeg(), []
    expect = _first("automatic", jpeg, _Both(_mask()))
    upload = MaskUpload(
        key=mask_key("automatic", CAPTURE, uuid.uuid4()), put_url="https://g/put"
    )
    frame = _frame(expect, mask=upload)
    out = await _activities(_Store(jpeg), puts=puts).backfill_prediction_mask(
        "automatic", frame
    )
    assert out.outcome == "reproduced" and out.prediction_id == frame.prediction_id
    ((url, data),) = puts
    assert url == "https://g/put" and out.mask.key == upload.key
    assert out.mask.sha256 == hashlib.sha256(data).hexdigest()
    assert (out.mask.width, out.mask.height) == (FRAME_W, FRAME_H)


async def test_a_mismatch_puts_nothing():
    jpeg, puts = _jpeg(), []
    expect = _first("automatic", jpeg, _Both(_mask()))
    frame = _frame(expect, mask=MaskUpload(key=mask_key("automatic", CAPTURE, uuid.uuid4()),
                                           put_url="https://g/put"))  # fmt: skip
    acts = _activities(_Store(jpeg), seg=_Both(_mask(half_len=260)), puts=puts)
    out = await acts.backfill_prediction_mask("automatic", frame)
    assert out.outcome == "mismatch" and out.mask is None and puts == []


async def test_the_headtail_jpeg_is_read_by_reference():
    jpeg = _jpeg()
    store = _Store(jpeg)
    ref = ObjectRef(bucket="fishsense-tenant-lab", key="tenants/x/jpegs/a.JPG")
    expect = _first("headtail", jpeg, _Both(_mask()))
    frame = BackfillMaskFrame(prediction_id=uuid.uuid4(), capture_id=CAPTURE,
                              dots=[list(DOT)], jpeg=ref, expect=expect)  # fmt: skip
    out = await _activities(store).backfill_prediction_mask("headtail", frame)
    assert out.outcome == "reproduced" and store.asked == [ref]


async def test_a_gone_image_is_said_not_raised():
    expect = MaskExpectation(mask_area_px=10, crop_x=0, crop_y=0)
    out = await _activities(_Store(None)).backfill_prediction_mask(
        "automatic", _frame(expect)
    )
    assert out.outcome == "image_unavailable"


async def test_no_gpu_fails_the_frame_without_a_retry():
    expect = MaskExpectation(mask_area_px=10, crop_x=0, crop_y=0)
    with pytest.raises(ApplicationError) as e:
        await _activities(_Store(_jpeg()), gpu=False).backfill_prediction_mask(
            "automatic", _frame(expect)
        )
    assert e.value.type == "NoGpuForSam3" and e.value.non_retryable
