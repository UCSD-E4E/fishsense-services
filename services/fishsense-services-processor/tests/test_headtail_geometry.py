"""Pure-logic tests for the head/tail predict stage's geometry.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_headtail_geometry.py. Everything here runs without a
GPU, a model or an image -- which is the point: these are the parts that fail
*plausibly* if they are wrong. A bad crop origin displaces every keypoint by a
constant and still looks like a fish; a bad silhouette ratio silently changes
which predictions get seeded.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.headtail_predict.geometry import (
    crop_origin,
    lift_point,
    mask_at_point,
    silhouette_ratio,
)

FRAME_W, FRAME_H = 4014, 3016
CROP_W, CROP_H = 1800, 1350


def test_crop_is_centred_on_the_laser_when_there_is_room():
    ox, oy = crop_origin(2000, 1500, FRAME_W, FRAME_H, CROP_W, CROP_H)
    assert (ox, oy) == (2000 - CROP_W // 2, 1500 - CROP_H // 2)


@pytest.mark.parametrize(
    "lx,ly",
    [
        (0, 0),
        (10, 10),
        (FRAME_W - 1, FRAME_H - 1),
        (FRAME_W - 5, 20),
        (20, FRAME_H - 5),
    ],
)
def test_crop_stays_inside_the_frame_at_every_edge_and_corner(lx, ly):
    """Clamped, never padded: padding shows the model a black band and
    truncating changes its aspect, both on exactly the edge frames."""
    ox, oy = crop_origin(lx, ly, FRAME_W, FRAME_H, CROP_W, CROP_H)
    assert 0 <= ox <= FRAME_W - CROP_W
    assert 0 <= oy <= FRAME_H - CROP_H


def test_crop_origin_handles_a_window_larger_than_the_frame():
    """Degenerate but must not produce a negative origin."""
    ox, oy = crop_origin(10, 10, 800, 600, 1800, 1350)
    assert (ox, oy) == (0, 0)


def test_lift_undoes_the_crop():
    """The one bug in this stage that looks entirely plausible in the output:
    every prediction displaced by the crop origin."""
    assert lift_point((10.0, 20.0), 1200, 900) == (1210.0, 920.0)


def test_lift_round_trips_with_crop_origin():
    lx, ly = 2500.0, 1800.0
    ox, oy = crop_origin(lx, ly, FRAME_W, FRAME_H, CROP_W, CROP_H)
    in_crop = (lx - ox, ly - oy)
    assert lift_point(in_crop, ox, oy) == (lx, ly)


def test_mask_at_point_picks_the_mask_covering_the_dot():
    """The gate: the laser says which fish, not "the biggest one"."""
    a = np.zeros((100, 100), dtype=np.uint8)
    a[0:10, 0:10] = 1
    b = np.zeros((100, 100), dtype=np.uint8)
    b[50:60, 50:60] = 1
    assert mask_at_point([a, b], [(55, 55)]) is b


def test_mask_at_point_returns_none_when_the_dot_is_on_background():
    a = np.zeros((100, 100), dtype=np.uint8)
    a[0:10, 0:10] = 1
    assert mask_at_point([a], [(80, 80)]) is None


def test_mask_at_point_tries_every_laser_point():
    """461 prod images carry two valid laser labels; first hit wins."""
    a = np.zeros((100, 100), dtype=np.uint8)
    a[50:60, 50:60] = 1
    assert mask_at_point([a], [(5, 5), (55, 55)]) is a


def test_mask_at_point_ignores_out_of_bounds_points():
    a = np.zeros((100, 100), dtype=np.uint8)
    a[50:60, 50:60] = 1
    assert mask_at_point([a], [(500, 500)]) is None


def test_silhouette_ratio_is_area_over_length_squared():
    assert silhouette_ratio(2500, 100.0) == pytest.approx(0.25)


def test_silhouette_ratio_is_none_for_a_degenerate_length():
    """A zero-length prediction has no meaningful shape; None rather than a
    division error or a misleading infinity."""
    assert silhouette_ratio(2500, 0.0) is None
