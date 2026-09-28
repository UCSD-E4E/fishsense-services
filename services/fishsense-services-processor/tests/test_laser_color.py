"""Reading the laser's colour off its own pixels.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_laser_color.py.

The laser LS config offers "Red Laser" and "Green Laser", and the pre-annotation
used to hardcode "Red Laser" on every frame. That is wrong for roughly a
quarter of prod: of the dives with completed laser labels, 143 are entirely
red and 88 entirely green (the 31 "mixed" ones carry a 1.2% minority, which is
labeler slips rather than a laser changing colour mid-dive).

So colour is a per-dive constant that nothing was tracking, and the dot itself
is the evidence. Measured over 332 human-labelled dots that still have a
processed JPEG in Garage: comparing R and G *at the dot* separates red from
green at 98.19%, and every one of the 6 misses is a frame where the labelled
point sits a pixel or two off the dot.

Two things that look like improvements and are not:

* Taking max(R-G) over a +-7px window scores 94.88% -- worse. A window that
  size is mostly background, and an underwater scene is blue-green, so the max
  finds the scene rather than the dot. Hence the brightest-pixels rule below:
  the dot is the bright thing in its own neighbourhood.
* Trusting any single frame. Populate takes the dive-level majority instead,
  which is what turns a 98% per-frame rule into 4/4 dives correct.

The coordinate detail is the one that would fail silently: the detector returns
*rectified* pixels, while `LinearRawImage` is in **sensor** coordinates. Sample
one at the other's coordinates and you read the wrong pixels -- near the laser
region that is tens of pixels, easily missing the dot entirely, and the result
still looks like a plausible colour.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.laser_predict.laser_color import (
    classify_laser_color,
    rectified_to_sensor_point,
)

_K = [[2833.0, 0.0, 2010.0], [0.0, 2858.0, 1450.0], [0.0, 0.0, 1.0]]
_D = [-0.28, 0.12, 0.0005, -0.0003, -0.03]


def _frame(h=400, w=400, bg=(90, 70, 60)):
    """A dim blue-green-ish background, like an underwater scene (BGR)."""
    img = np.zeros((h, w, 3), np.uint16)
    img[:, :] = bg
    return img


def _dot(img, x, y, bgr, radius=4):
    ys, xs = np.ogrid[: img.shape[0], : img.shape[1]]
    img[((xs - x) ** 2 + (ys - y) ** 2) <= radius**2] = bgr
    return img


# --- coordinate mapping -----------------------------------------------------


@pytest.mark.parametrize(
    "sensor_xy",
    [(2010.0, 1450.0), (1600.0, 600.0), (2400.0, 1900.0), (1000.0, 300.0)],
)
def test_rectified_to_sensor_inverts_the_undistort(sensor_xy):
    """Round-trip against OpenCV's own forward transform.

    `undistortPoints(..., P=K)` is precisely what maps a sensor pixel into the
    rectified frame the detector reports in, so mapping back has to land on the
    pixel we started from.
    """
    import cv2

    src = np.array([[list(sensor_xy)]], dtype=np.float64)
    rectified = cv2.undistortPoints(
        src, np.array(_K), np.array(_D), P=np.array(_K)
    ).reshape(2)
    back = rectified_to_sensor_point(float(rectified[0]), float(rectified[1]), _K, _D)
    assert back == pytest.approx(sensor_xy, abs=0.5)


def test_mapping_actually_moves_the_point():
    """A no-op mapping would pass the round-trip test above and still be
    wrong. At the laser region the distortion shift is tens of pixels --
    enough to miss the dot completely."""
    # The region's top-left corner, where the shift is ~48 px. It is only
    # ~5 px near the frame centre, so a test point matters here.
    x, y = 1580.0, 395.0
    sx, sy = rectified_to_sensor_point(x, y, _K, _D)
    assert np.hypot(sx - x, sy - y) > 10.0


def test_zero_distortion_is_the_identity():
    assert rectified_to_sensor_point(1234.0, 567.0, _K, [0.0] * 5) == pytest.approx(
        (1234.0, 567.0), abs=1e-6
    )


# --- colour classification --------------------------------------------------


def test_reads_a_red_dot():
    img = _dot(_frame(), 200, 200, (40, 60, 4000))
    color, margin = classify_laser_color(img, 200, 200)
    assert color == "red" and margin > 0


def test_reads_a_green_dot():
    img = _dot(_frame(), 200, 200, (40, 4000, 60))
    color, margin = classify_laser_color(img, 200, 200)
    assert color == "green" and margin < 0


def test_tolerates_the_point_being_a_couple_of_pixels_off():
    """The production caller passes a *predicted* point, not a human label."""
    img = _dot(_frame(), 200, 200, (40, 4000, 60))
    assert classify_laser_color(img, 202, 198)[0] == "green"


def test_is_not_fooled_by_a_blue_green_background():
    """An underwater scene is greenish everywhere; only the dot counts. This
    is the case the +-7px max-over-window rule got wrong."""
    img = _frame(bg=(400, 800, 120))
    _dot(img, 200, 200, (200, 300, 4000))
    assert classify_laser_color(img, 200, 200)[0] == "red"


def test_returns_none_outside_the_frame():
    assert classify_laser_color(_frame(), 5000, 5000) == (None, None)


def test_saturated_white_core_still_reads_from_the_halo():
    """A close dot blows out to white, which carries no colour. The halo
    around it does -- 4 of the 6 misses in the corpus look like this."""
    img = _frame()
    _dot(img, 200, 200, (300, 3000, 300), radius=7)  # green halo
    _dot(img, 200, 200, (65535, 65535, 65535), radius=3)  # blown core
    assert classify_laser_color(img, 200, 200)[0] == "green"


def test_margin_scales_with_separation():
    """A washed-out dot must report a smaller margin than a vivid one, so a
    close call can be recognised as one."""
    vivid = classify_laser_color(_dot(_frame(), 200, 200, (40, 60, 4000)), 200, 200)[1]
    washed = classify_laser_color(_dot(_frame(), 200, 200, (300, 380, 460)), 200, 200)[
        1
    ]
    assert vivid > washed > 0


def test_uint8_and_uint16_agree():
    """The JPEG corpus this was tuned on is 8-bit; production samples 16-bit
    linear. The call must not depend on the container's scale."""
    small = _dot(np.full((400, 400, 3), 60, np.uint8), 200, 200, (10, 15, 250))
    big = _dot(_frame(bg=(60, 60, 60)), 200, 200, (2560, 3840, 64000))
    assert classify_laser_color(small, 200, 200)[0] == "red"
    assert classify_laser_color(big, 200, 200)[0] == "red"
