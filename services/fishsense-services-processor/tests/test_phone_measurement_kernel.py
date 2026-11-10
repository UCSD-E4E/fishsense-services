"""The phone measurement kernel: FishSense Mobile's own sequence, on the server.

The control is a fish with a known answer: an ellipse mask on a
fronto-parallel plane at a known depth, whose length is the distance between
its two ends back-projected at that depth. Segmentation is a stub that hands
the kernel that mask, as the app's tests stub nothing but the model.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
import pytest
from PIL import Image

from fishsense_services_processor.phone_measurement.kernel import (
    decode_bgr,
    measure,
    read_depth,
    remeasure,
)

W, H = 1920, 1440
DEPTH_W, DEPTH_H = 256, 192
#: CCFRP's iPad (fishsense-mobile-oceans-2025@e769bdd, 01_process.ipynb cell 4).
K = (1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0)
DEPTH_M = 0.5
CENTER, SEMI = (960, 720), (400, 120)


def _fish_mask() -> np.ndarray:
    mask = np.zeros((H, W), np.uint8)
    cv2.ellipse(mask, CENTER, SEMI, 0, 0, 360, 255, -1)
    return mask


def _plane(depth: float = DEPTH_M) -> np.ndarray:
    return np.full((DEPTH_H, DEPTH_W), depth, np.float32)


def _expected_length(head, tail, depth: float = DEPTH_M) -> float:
    k_inv = np.linalg.inv(np.array(K).reshape(3, 3))
    a = k_inv @ np.array([head[0], head[1], 1.0]) * depth
    b = k_inv @ np.array([tail[0], tail[1], 1.0]) * depth
    return float(np.linalg.norm(a - b))


class _Segmenter:
    """`FishSegmentation`'s two calls the kernel makes."""

    def __init__(self, mask):
        self.mask = mask
        self.seen = None

    def inference_single(self, img):
        self.seen = img
        return self.mask

    def model_id(self):
        return "fishial/test@000000000000"


def _bgr() -> np.ndarray:
    return np.full((H, W, 3), 60, np.uint8)


def test_the_control_measures_its_known_length():
    got = measure(_bgr(), _plane(), K, segmenter=_Segmenter(_fish_mask()))
    assert got.status == "measured"
    # The ends of the ellipse's long axis, as the app's head/tail finds them.
    assert got.snout[0] == pytest.approx(CENTER[0] - SEMI[0], abs=2)
    assert got.fork[0] == pytest.approx(CENTER[0] + SEMI[0], abs=2)
    assert got.snout_depth_m == pytest.approx(DEPTH_M, rel=1e-4)
    assert got.fork_depth_m == pytest.approx(DEPTH_M, rel=1e-4)
    # The known answer: about 2a * z / f = 800 * 0.5 / 1604 = 0.249 m.
    assert got.length_m == pytest.approx(
        _expected_length(got.snout, got.fork), rel=1e-5
    )
    assert got.length_m == pytest.approx(800 * DEPTH_M / K[0], rel=0.01)
    assert got.model_id == "fishial/test@000000000000"
    assert got.mask is not None and got.mask.shape == (H, W)


def test_length_scales_with_depth():
    near = measure(_bgr(), _plane(0.5), K, segmenter=_Segmenter(_fish_mask()))
    far = measure(_bgr(), _plane(1.0), K, segmenter=_Segmenter(_fish_mask()))
    assert far.length_m == pytest.approx(2 * near.length_m, rel=1e-4)


def test_each_point_takes_its_own_depth_on_a_tilted_plane():
    """A fish across a plane that recedes left to right: the snout (left) is
    nearer than the fork, and each is back-projected at its own depth."""
    cols = np.linspace(0.4, 0.8, DEPTH_W, dtype=np.float32)
    tilted = np.tile(cols, (DEPTH_H, 1))
    got = measure(_bgr(), tilted, K, segmenter=_Segmenter(_fish_mask()))
    assert got.status == "measured"
    assert got.snout_depth_m < got.fork_depth_m - 0.05
    k_inv = np.linalg.inv(np.array(K).reshape(3, 3))
    a = k_inv @ np.array([*got.snout, 1.0]) * got.snout_depth_m
    b = k_inv @ np.array([*got.fork, 1.0]) * got.fork_depth_m
    assert got.length_m == pytest.approx(float(np.linalg.norm(a - b)), rel=1e-5)


def test_no_fish_is_a_status_not_an_error():
    got = measure(_bgr(), _plane(), K, segmenter=_Segmenter(None))
    assert got.status == "no_fish"
    assert got.length_m is None and got.mask is None
    assert got.model_id == "fishial/test@000000000000"


def test_segmentation_sees_the_frame_as_bgr_pixels():
    bgr = np.zeros((H, W, 3), np.uint8)
    bgr[..., 0] = 200  # blue
    seg = _Segmenter(_fish_mask())
    measure(bgr, _plane(), K, segmenter=seg)
    assert seg.seen.shape == (H, W, 3) and seg.seen.dtype == np.uint8
    assert seg.seen[0, 0].tolist() == [200, 0, 0]


def test_a_jpeg_decodes_to_bgr_without_its_exif_rotation():
    """The app segments the camera's own frame; a JPEG's orientation tag
    would turn it out of the depth map's grid."""
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[..., 0] = 220  # red, in RGB
    out = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 on display
    Image.fromarray(rgb).save(out, "JPEG", quality=95, exif=exif)
    bgr = decode_bgr(out.getvalue())
    assert bgr.shape == (H, W, 3)
    b, g, r = bgr[H // 2, W // 2].tolist()
    assert r > 200 and b < 30


def test_an_undecodable_jpeg_is_none():
    assert decode_bgr(b"not a jpeg") is None


def test_depth_is_read_with_its_row_stride():
    """ARKit pads depth rows (`depth_row_bytes`); the pad is not depth."""
    depth = np.arange(DEPTH_W * DEPTH_H, dtype=np.float32).reshape(DEPTH_H, DEPTH_W)
    stride = DEPTH_W * 4 + 64
    padded = bytearray()
    for row in depth:
        padded += row.tobytes() + b"\xff" * 64
    got = read_depth(bytes(padded), DEPTH_W, DEPTH_H, stride)
    assert got.dtype == np.float32 and got.shape == (DEPTH_H, DEPTH_W)
    assert np.array_equal(got, depth)
    assert np.array_equal(
        read_depth(depth.tobytes(), DEPTH_W, DEPTH_H, DEPTH_W * 4), depth
    )


def test_depth_of_the_wrong_size_is_refused():
    with pytest.raises(ValueError):
        read_depth(b"\x00" * 100, DEPTH_W, DEPTH_H, DEPTH_W * 4)


def test_a_stride_narrower_than_a_row_is_refused():
    """Bytes that fit the stride but not the width would drop columns."""
    stride = DEPTH_W * 4 - 4
    with pytest.raises(ValueError):
        read_depth(b"\x00" * (stride * DEPTH_H), DEPTH_W, DEPTH_H, stride)


def test_remeasure_keeps_the_points_it_is_given():
    """The app's recompute after an edit: the mask and depth as they were,
    the person's snout and fork, never moved."""
    snout, fork = (600.4, 700.6), (1300.3, 740.7)
    got = remeasure(_fish_mask(), _plane(), K, snout, fork)
    assert got.status == "measured"
    assert got.snout == snout and got.fork == fork
    assert got.length_m == pytest.approx(_expected_length(snout, fork), rel=1e-5)
