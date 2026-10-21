"""SAM masks stored as files (contract 12, `fishsense_services_contracts.masks`).

New in v2 (2026-10-21). The mask SAM kept was dropped once its head/tail, box
and area were taken. Both stages now place it in a frame-sized uint8 array --
the app's ``mask_u8``: width x height, 255 on the fish -- gzip it, and PUT it
to the key the orchestrator presigned, reporting its size and SHA-256. A frame
with no upload slot, or no kept mask, stores nothing.
"""

from __future__ import annotations

import gzip
import hashlib
import uuid

import numpy as np
import pytest

from fishsense_services_contracts.masks import MASK_CONTENT_TYPE, MaskUpload, mask_key
from fishsense_services_processor.masks import FrameMask, MaskSink, store_mask

KEY = mask_key("automatic", uuid.UUID(int=2), uuid.UUID(int=1))


def _crop(h=4, w=6):
    crop = np.zeros((h, w), np.uint8)
    crop[1:3, 2:5] = 1
    return crop


def test_a_mask_is_the_apps_encoding_at_the_frame_gzipd():
    mask = FrameMask(crop=_crop(), x=10, y=20, width=40, height=30)

    data = mask.encode()
    pixels = np.frombuffer(gzip.decompress(data), np.uint8).reshape(30, 40)

    expected = np.zeros((30, 40), np.uint8)
    expected[21:23, 12:15] = 255
    assert (pixels == expected).all()
    # No timestamp in the gzip header (bytes 4-7): the same mask is always the
    # same bytes, and the same SHA-256.
    assert data[4:8] == bytes(4)


def test_a_crop_past_the_frame_edge_is_clipped_to_the_frame():
    crop = np.ones((10, 10), np.uint8)
    pixels = np.frombuffer(
        gzip.decompress(FrameMask(crop=crop, x=35, y=25, width=40, height=30).encode()),
        np.uint8,
    ).reshape(30, 40)
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
