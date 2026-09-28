"""Pure-logic tests for the stage-0.1 laser-region overlay + JPEG encode.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_overlay_laser_region_and_encode_jpeg.py.

The expected-laser region became a convex polygon on 2026-08-27: the union of
the per-rig epipolar streaks is not axis-aligned, so a rectangle spent a
quarter of its area on corners the laser cannot reach. The rectangle path is
still here and still tested (`test_overlay_laser_bbox_and_encode_jpeg.py`) --
it is the fallback for a data-worker/api-worker version skew, and it is what
`test_stage0_1_notebook_parity.py` pins byte-for-byte against the notebook.
These tests cover the polygon path without rawpy or Temporal.
"""

import cv2
import numpy as np

from fishsense_services_processor.laser_preprocess.overlay import (
    overlay_laser_bbox_and_encode_jpeg,
    overlay_laser_region_and_encode_jpeg,
)

# The production region (api-worker `LASER_REGION_POLYGON`), scaled to fit the
# synthetic frames below. Shape matters here, not the exact vertices -- those
# are pinned on the api-worker side, where they are derived.
_REGION = [
    [1580, 570],
    [1700, 465],
    [2335, 395],
    [2455, 525],
    [2470, 1610],
    [2185, 1890],
    [1920, 1905],
    [1625, 1365],
]


def _make_image(height: int = 3000, width: int = 4000) -> np.ndarray:
    return np.full((height, width, 3), fill_value=128, dtype=np.uint8)


def _decode(out: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)


def test_returns_non_empty_jpeg_bytes():
    out = overlay_laser_region_and_encode_jpeg(_make_image(), _REGION)
    assert out[:2] == b"\xff\xd8"
    assert len(out) > 1024


def test_decoded_jpeg_keeps_input_shape():
    out = overlay_laser_region_and_encode_jpeg(_make_image(2000, 3000), _REGION)
    assert _decode(out).shape == (2000, 3000, 3)


def test_does_not_mutate_input_array():
    img = _make_image()
    original = img.copy()
    overlay_laser_region_and_encode_jpeg(img, _REGION)
    assert np.array_equal(img, original), "input array was mutated"


def test_every_edge_of_the_polygon_is_drawn():
    """Not just the vertices: a closed polyline that silently dropped its
    last edge would still put green at every corner."""
    decoded = _decode(overlay_laser_region_and_encode_jpeg(_make_image(), _REGION))
    for i, start in enumerate(_REGION):
        end = _REGION[(i + 1) % len(_REGION)]
        mid = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
        patch = decoded[mid[1] - 3 : mid[1] + 4, mid[0] - 3 : mid[0] + 4]
        blue, green, red = patch[..., 0].max(), patch[..., 1].max(), patch[..., 2].max()
        assert green > blue and green > red, f"edge {i}->{i+1} not drawn at {mid}"


def test_polygon_corners_are_not_filled():
    """The region is an outline, not a mask -- a filled polygon would hide the
    image underneath it, which is the one thing the labeler needs to see."""
    decoded = _decode(overlay_laser_region_and_encode_jpeg(_make_image(), _REGION))
    centre = (
        sum(v[0] for v in _REGION) // len(_REGION),
        sum(v[1] for v in _REGION) // len(_REGION),
    )
    blue, green, red = decoded[centre[1], centre[0]]
    assert (
        abs(int(green) - 128) < 20
        and abs(int(blue) - 128) < 20
        and abs(int(red) - 128) < 20
    )


def test_polygon_and_its_bounding_box_differ():
    """Guards the degenerate 'fix' of drawing the AABB and calling it a
    region -- the whole point is that they are not the same shape."""
    img = _make_image()
    xs = [v[0] for v in _REGION]
    ys = [v[1] for v in _REGION]
    poly = overlay_laser_region_and_encode_jpeg(img, _REGION)
    rect = overlay_laser_bbox_and_encode_jpeg(img, (min(xs), min(ys), max(xs), max(ys)))
    assert poly != rect


def test_three_vertex_region_is_accepted():
    """Nothing in the drawing depends on the vertex count -- the constant is
    8-sided today, and re-measuring it must not need a code change."""
    out = overlay_laser_region_and_encode_jpeg(
        _make_image(), [[100, 100], [900, 200], [500, 800]]
    )
    assert _decode(out).shape == (3000, 4000, 3)
