"""SAM masks stored as files (contract 12, `fishsense_services_contracts.masks`).

New in v2 (2026-10-21). The mask SAM kept was dropped once its head/tail, box
and area were taken. Both stages now place it in a frame-sized array -- the
app's ``mask_png`` (#230): 8-bit greyscale PNG, width x height, 255 on the
fish -- and PUT it to the key the orchestrator presigned, reporting its size
and SHA-256. A frame with no upload slot, or no kept mask, stores nothing.
"""

from __future__ import annotations

import hashlib
import uuid

import cv2
import numpy as np
import pytest

from fishsense_services_contracts.masks import MASK_CONTENT_TYPE, MaskUpload, mask_key
from fishsense_services_processor.masks import FrameMask, MaskSink, store_mask

KEY = mask_key("automatic", uuid.UUID(int=2), uuid.UUID(int=1))


def _crop(h=4, w=6):
    crop = np.zeros((h, w), np.uint8)
    crop[1:3, 2:5] = 1
    return crop


def _decoded(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)


def test_a_mask_is_the_apps_png_at_the_frame():
    mask = FrameMask(crop=_crop(), x=10, y=20, width=40, height=30)

    data = mask.encode()
    pixels = _decoded(data)

    expected = np.zeros((30, 40), np.uint8)
    expected[21:23, 12:15] = 255
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR: 8-bit (byte 24), greyscale (colour type 0, byte 25).
    assert (data[24], data[25]) == (8, 0)
    assert pixels.dtype == np.uint8 and pixels.shape == (30, 40)
    assert (pixels == expected).all()
    assert mask.encode() == data  # the same mask, the same bytes


def test_a_crop_past_the_frame_edge_is_clipped_to_the_frame():
    crop = np.ones((10, 10), np.uint8)
    pixels = _decoded(FrameMask(crop=crop, x=35, y=25, width=40, height=30).encode())
    assert pixels[25:, 35:].all() and pixels.sum() == 255 * 5 * 5


def test_a_stored_mask_names_what_was_put():
    puts = []

    def put(url, headers, data):
        puts.append((url, headers, data))
        return 200

    mask = FrameMask(crop=_crop(), x=0, y=0, width=8, height=6)
    stored = store_mask(put, MaskUpload(key=KEY, put_url="https://g/put"), mask)

    ((url, headers, data),) = puts
    assert url == "https://g/put" and headers == {"Content-Type": MASK_CONTENT_TYPE}
    assert data == mask.encode()
    assert (stored.key, stored.size, stored.sha256, stored.width, stored.height) == (
        KEY, len(data), hashlib.sha256(data).hexdigest(), 8, 6,
    )  # fmt: skip


def test_a_put_the_store_refuses_fails_the_frame():
    with pytest.raises(RuntimeError):
        store_mask(lambda *a: 403, MaskUpload(key=KEY, put_url="u"),
                   FrameMask(crop=_crop(), x=0, y=0, width=8, height=6))  # fmt: skip


def test_a_sink_holds_the_kept_mask_once():
    sink = MaskSink()
    assert sink.mask is None
    sink.keep(_crop(), 3, 4, 40, 30)
    assert (sink.mask.x, sink.mask.y, sink.mask.width, sink.mask.height) == (
        3, 4, 40, 30,
    )  # fmt: skip
