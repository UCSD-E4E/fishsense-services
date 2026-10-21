"""Checking a stored SAM mask before it is recorded (contract 12, 0058).

New in v2 (2026-10-21). The processor PUTs each kept mask -- the app's
``mask_png`` (#230): 8-bit greyscale PNG at the frame -- to a key the
orchestrator presigned, and reports its size and SHA-256. It runs on machines
we don't own, so before a mask is named against its prediction the
orchestrator reads it back: the key one of this stage's for this result's
capture, the bytes the ones reported, the PNG header 8-bit greyscale at the
frame. A mask that fails is left unrecorded and deleted; its prediction still
stands. Review of #232: the masks come back parallel to the results, the
reads run several at once, and nothing is decompressed.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid

import cv2
import numpy as np
import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.automatic_results import AutomaticFrameResult
from fishsense_services_contracts.masks import StoredMask, mask_key
from fishsense_services_orchestrator import masks as sut

CAPTURE = uuid.uuid4()
W, H = 40, 30


class _Objects:
    bucket = "fishsense-tenant-lab"

    def __init__(self, stored=None, error=None, delay=0.0):
        self.stored = stored or {}
        self.error = error
        self.delay = delay
        self.deleted = []
        self._lock = threading.Lock()
        self.in_flight = self.most = 0

    def presign_mask_put(self, key):
        return type("U", (), {"url": f"https://g/{key}?sig"})()

    def get(self, key):
        with self._lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
        try:
            time.sleep(self.delay)
            if self.error:
                raise self.error
            return self.stored.get(key)
        finally:
            with self._lock:
                self.in_flight -= 1

    def delete(self, key):
        self.deleted.append(key)


def _png(w=W, h=H, dtype=np.uint8, channels=None):
    shape = (h, w) if channels is None else (h, w, channels)
    return cv2.imencode(".png", np.zeros(shape, dtype))[1].tobytes()


def _result(data=None, *, capture=CAPTURE, key=None, size=None, sha=None,
            width=W, height=H, frame=(W, H)):  # fmt: skip
    data = _png() if data is None else data
    key = key or mask_key("automatic", capture, uuid.uuid4())
    mask = StoredMask(key=key, size=size or len(data),
                      sha256=sha or hashlib.sha256(data).hexdigest(),
                      width=width, height=height)  # fmt: skip
    return AutomaticFrameResult(capture_id=capture, status="predicted",
                                predictor_version=3, width=frame[0],
                                height=frame[1], mask=mask), data  # fmt: skip


async def _checked(objects, results):
    return await ActivityEnvironment().run(
        sut.checked_masks, objects, "automatic", results
    )


def test_an_upload_is_a_new_key_of_this_stage_and_capture_presigned():
    objects = _Objects()
    one, two = (sut.mask_upload(objects, "automatic", CAPTURE) for _ in range(2))
    assert one.key != two.key
    assert one.key.startswith(f"masks/automatic/{CAPTURE}/") and one.key.endswith(
        ".png"
    )
    assert one.put_url == f"https://g/{one.key}?sig"


def test_a_pngs_size_comes_from_its_header():
    assert sut.png_size(_png(40, 30)) == (40, 30, 8, 0)
    assert sut.png_size(_png(40, 30, np.uint16)) == (40, 30, 16, 0)
    assert sut.png_size(_png(40, 30, channels=3)) == (40, 30, 8, 2)
    assert sut.png_size(b"not a png at all, not one bit") is None


async def test_a_mask_read_back_as_reported_is_recorded_at_its_place():
    r, data = _result()
    empty = AutomaticFrameResult(capture_id=uuid.uuid4(), status="no_detections",
                                 predictor_version=3)  # fmt: skip

    masks, failed = await _checked(_Objects({r.mask.key: data}), [empty, r])

    assert failed == [] and masks[0] is None
    m = masks[1]
    assert (m.capture_id, m.bucket, m.key, m.sha256, m.size) == (
        CAPTURE, "fishsense-tenant-lab", r.mask.key, r.mask.sha256, len(data),
    )  # fmt: skip
    assert (m.content_type, m.width, m.height) == ("image/png", W, H)


def _bad_cases():
    other = mask_key("automatic", uuid.uuid4(), uuid.uuid4())
    return {
        "another capture's key": dict(key=other),
        "another stage's key": dict(key=mask_key("headtail", CAPTURE, uuid.uuid4())),
        "not the frame": dict(frame=(W + 1, H)),
        "other size": dict(size=999),
        "other hash": dict(sha="0" * 64),
        "a PNG of another size": dict(data=_png(W - 1, H)),
        "16-bit": dict(data=_png(dtype=np.uint16)),
        "colour": dict(data=_png(channels=3)),
        "not a PNG": dict(data=bytes(W * H)),
    }


@pytest.mark.parametrize("case", sorted(_bad_cases()))
async def test_a_mask_that_does_not_check_out_is_not_recorded(case):
    r, data = _result(**_bad_cases()[case])
    masks, failed = await _checked(_Objects({r.mask.key: data}), [r])
    assert masks == [None] and failed == [r.mask.key]


async def test_a_mask_not_there_or_unreadable_is_not_recorded():
    r, _ = _result()
    for objects in (_Objects({}), _Objects(error=RuntimeError("x"))):
        masks, failed = await _checked(objects, [r])
        assert masks == [None] and failed == [r.mask.key]


async def test_masks_are_read_several_at_once_and_no_more():
    """Review of #232: one at a time, a page of masks took minutes inside one
    persist."""
    pairs = [_result(capture=uuid.uuid4()) for _ in range(3 * sut.READS_AT_ONCE)]
    objects = _Objects({r.mask.key: d for r, d in pairs}, delay=0.02)

    masks, _ = await _checked(objects, [r for r, _ in pairs])

    assert all(m is not None for m in masks)
    assert 1 < objects.most <= sut.READS_AT_ONCE


async def test_masks_that_will_never_be_recorded_are_deleted():
    objects = _Objects(error=None)
    await ActivityEnvironment().run(sut.discard, objects, ["a", "b"])
    assert objects.deleted == ["a", "b"]
