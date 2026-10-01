"""The one JPEG encoder every processor stage writes with.

v1 had four copies of the same five lines (`encode_rectified_jpeg`, the laser
overlay's `_encode_jpeg`, the species overlay's tail, the checkerboard's local
copy). They all call `cv2.imencode(".jpg")` at OpenCV's default quality, so a
JPEG v2 writes is byte-identical to the one v1 wrote for the same pixels.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from fishsense_services_processor import jpeg


def _image() -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (64, 96, 3), dtype=np.uint8)


def test_it_is_v1s_encoding_byte_for_byte():
    img = _image()
    ok, expected = cv2.imencode(".jpg", img)
    assert ok
    assert jpeg.encode_jpeg(img) == expected.tobytes()


def test_it_does_not_mutate_its_argument():
    img = _image()
    before = img.copy()
    jpeg.encode_jpeg(img)
    assert np.array_equal(img, before)


def test_a_failed_encode_raises_rather_than_writing_nothing(monkeypatch):
    monkeypatch.setattr(jpeg.cv2, "imencode", lambda *a: (False, None))
    with pytest.raises(RuntimeError, match="imencode"):
        jpeg.encode_jpeg(_image())


@pytest.mark.parametrize(
    "module, name",
    [
        ("fishsense_services_processor.checkerboard.activities", "encode_jpeg"),
        (
            "fishsense_services_processor.headtail_preprocess.activities",
            "encode_rectified_jpeg",
        ),
        ("fishsense_services_processor.laser_preprocess.overlay", "_encode_jpeg"),
    ],
)
def test_every_stage_encodes_through_it(module, name):
    import importlib

    assert getattr(importlib.import_module(module), name) is jpeg.encode_jpeg
