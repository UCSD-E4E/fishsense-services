"""Checking a stored SAM mask before it is recorded (contract 12, 0058).

New in v2 (2026-10-21). The processor PUTs each kept mask to a key the
orchestrator presigned and reports its size and SHA-256; it runs on machines
we don't own, so before a mask is named against its prediction the
orchestrator reads it back: the key must be one of this stage's for this
result's capture, the bytes the ones reported, and gunzipped they must be the
app's ``mask_u8`` for the frame (``width x height``). A mask that fails is
left unrecorded and logged -- its prediction still stands.
"""

from __future__ import annotations

import gzip
import hashlib
import uuid

import pytest

from fishsense_services_contracts.automatic_results import AutomaticFrameResult
from fishsense_services_contracts.masks import StoredMask, mask_key
from fishsense_services_orchestrator.masks import checked_masks, mask_upload

CAPTURE = uuid.uuid4()
W, H = 40, 30


class _Objects:
    bucket = "fishsense-tenant-lab"

    def __init__(self, stored=None, error=None):
        self.stored = stored or {}
        self.error = error
        self.signed = []

    def presign_mask_put(self, key):
        self.signed.append(key)
        return type("U", (), {"url": f"https://g/{key}?sig"})()

    def get(self, key):
        if self.error:
            raise self.error
        return self.stored.get(key)


def _data(w=W, h=H):
    return gzip.compress(bytes(w * h), mtime=0)


def _result(data=None, *, capture=CAPTURE, key=None, size=None, sha=None,
            width=W, height=H, frame=(W, H)):  # fmt: skip
    data = _data() if data is None else data
    key = key or mask_key("automatic", capture, uuid.uuid4())
    mask = StoredMask(key=key, size=size or len(data),
                      sha256=sha or hashlib.sha256(data).hexdigest(),
                      width=width, height=height)  # fmt: skip
    return AutomaticFrameResult(capture_id=capture, status="predicted",
                                predictor_version=3, width=frame[0],
                                height=frame[1], mask=mask), data  # fmt: skip


def test_an_upload_is_a_new_key_of_this_stage_and_capture_presigned():
    objects = _Objects()
    one, two = (mask_upload(objects, "automatic", CAPTURE) for _ in range(2))
    assert one.key != two.key
    assert one.key.startswith(f"masks/automatic/{CAPTURE}/")
    assert one.put_url == f"https://g/{one.key}?sig"


async def test_a_mask_read_back_as_reported_is_recorded():
    r, data = _result()
    (m,) = await checked_masks(_Objects({r.mask.key: data}), "automatic", [r])
    assert (m.capture_id, m.bucket, m.key, m.sha256, m.size) == (
        CAPTURE, "fishsense-tenant-lab", r.mask.key, r.mask.sha256, len(data),
    )  # fmt: skip
    assert (m.content_type, m.width, m.height) == ("application/gzip", W, H)


def _bad_cases():
    other = mask_key("automatic", uuid.uuid4(), uuid.uuid4())
    return {
        "another capture's key": dict(key=other),
        "another stage's key": dict(key=mask_key("headtail", CAPTURE, uuid.uuid4())),
        "not the frame": dict(frame=(W + 1, H)),
        "other size": dict(size=999),
        "other hash": dict(sha="0" * 64),
        "not width x height": dict(data=gzip.compress(bytes(W * H - 1), mtime=0)),
        "not gzip": dict(data=bytes(W * H)),
    }


@pytest.mark.parametrize("case", sorted(_bad_cases()))
async def test_a_mask_that_does_not_check_out_is_not_recorded(case):
    r, data = _result(**_bad_cases()[case])
    assert await checked_masks(_Objects({r.mask.key: data}), "automatic", [r]) == []


async def test_a_mask_not_there_or_unreadable_is_not_recorded():
    r, _ = _result()
    assert await checked_masks(_Objects({}), "automatic", [r]) == []
    assert (
        await checked_masks(_Objects(error=RuntimeError("x")), "automatic", [r]) == []
    )


async def test_results_with_no_mask_are_passed_over():
    r = AutomaticFrameResult(capture_id=CAPTURE, status="no_detections",
                             predictor_version=3)  # fmt: skip
    assert await checked_masks(_Objects(), "automatic", [r]) == []
