"""Pure-logic tests for stage 0.1 laser-bbox overlay + JPEG encode.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_overlay_laser_bbox_and_encode_jpeg.py.

Stage 0.1's per-image transform draws a thin green rectangle around the
expected laser region of a rectified image and encodes to JPEG. These
tests cover the overlay+encode step without rawpy or Temporal — a
synthetic numpy array is enough.
"""

import cv2
import numpy as np

from fishsense_services_processor.laser_preprocess.overlay import (
    overlay_laser_bbox_and_encode_jpeg,
)

# Defaults that match the original notebook's hard-coded bounding box.
_DEFAULT_BBOX = (1800, 700, 2400, 1600)


def _make_image(height: int = 3000, width: int = 4000) -> np.ndarray:
    return np.full((height, width, 3), fill_value=128, dtype=np.uint8)


def test_returns_non_empty_jpeg_bytes():
    img = _make_image()
    out = overlay_laser_bbox_and_encode_jpeg(img, _DEFAULT_BBOX)
    assert out[:2] == b"\xff\xd8"
    assert len(out) > 1024


def test_decoded_jpeg_keeps_input_shape():
    img = _make_image(2000, 3000)
    out = overlay_laser_bbox_and_encode_jpeg(img, _DEFAULT_BBOX)
    decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (2000, 3000, 3)


def test_does_not_mutate_input_array():
    img = _make_image()
    original = img.copy()
    overlay_laser_bbox_and_encode_jpeg(img, _DEFAULT_BBOX)
    assert np.array_equal(img, original), "input array was mutated"


def test_bbox_pixels_are_green_in_decoded_jpeg():
    img = _make_image()
    out = overlay_laser_bbox_and_encode_jpeg(img, _DEFAULT_BBOX)
    decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)

    # Sample a point on the top edge of the rectangle. The rectangle is
    # drawn with thickness 2 so a pixel near the line should be heavily
    # biased toward green channel after JPEG round-trip.
    x_center = (_DEFAULT_BBOX[0] + _DEFAULT_BBOX[2]) // 2
    y_top = _DEFAULT_BBOX[1]
    b, g, r = decoded[y_top, x_center]
    assert g > b and g > r, f"top edge pixel not greenish: {decoded[y_top, x_center]}"


def test_different_bboxes_produce_different_outputs():
    img = _make_image()
    a = overlay_laser_bbox_and_encode_jpeg(img, _DEFAULT_BBOX)
    b = overlay_laser_bbox_and_encode_jpeg(img, (100, 100, 500, 500))
    assert a != b
