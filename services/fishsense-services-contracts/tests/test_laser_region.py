"""The stage-0.1 expected-laser region has to cover every rig, not just one.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
test_laser_region.py. Test names, fixtures and reasons are v1's; v2 change:
plain Python rather than numpy (the contract package carries no array library,
and v1's predicate was already written without one so both workers could call
it).

Stage 0.1 runs *before* the dive has a laser calibration of its own --
calibration is stage 13 -- so `LASER_REGION_POLYGON` is a single constant
applied to every dive, and the only way to know it is right is to measure the
rig population it has to cover. Two independent legs, because neither is
sufficient alone:

* `PROD_CALIBRATIONS` is the geometry: each fitted laser ray projected across
  the working depth range. Exact, but a sample of 13.
* `PROD_DIVE_LASER_LOCI` is the observation, and it is what caught the bug: the
  uncalibrated rigs sit measurably further right than the calibrated 13.

Corpus measured 2026-08-27 against prod: 13 `LaserExtrinsics` rows and the
31,322 completed non-superseded `LaserLabel` rows spanning 262 dives.
"""

from __future__ import annotations

import math

import pytest

from fishsense_services_contracts.laser_region import (
    DEFAULT_LASER_BBOX,
    LASER_REGION_POLYGON,
    WORKING_DEPTH_RANGE_M,
    point_in_laser_region,
)

# (laser_origin_xy, laser_axis, fx, fy, cx, cy) for all 13 prod calibrations.
PROD_CALIBRATIONS = [
    ((-0.030078, -0.099278), (0.014452, 0.038441, 0.999156), 2833.28, 2858.77, 2009.76, 1407.99),
    ((-0.032060, -0.099302), (0.001308, 0.027590, 0.999618), 2832.89, 2857.55, 2027.31, 1492.53),
    ((-0.031704, -0.099556), (0.026947, 0.029967, 0.999188), 2827.30, 2852.24, 1993.26, 1443.37),
    ((-0.031174, -0.100156), (0.031329, 0.047707, 0.998370), 2855.29, 2881.07, 2031.79, 1447.69),
    ((-0.030574, -0.099056), (0.005685, 0.032688, 0.999449), 2832.66, 2855.88, 1968.03, 1452.81),
    ((-0.029878, -0.100020), (0.022015, 0.027924, 0.999368), 2825.15, 2850.61, 2061.85, 1458.56),
    ((-0.029473, -0.099408), (0.029015, 0.013736, 0.999485), 2841.37, 2863.76, 2004.36, 1476.25),
    ((-0.029347, -0.098302), (0.001327, 0.026194, 0.999656), 2833.28, 2858.77, 2009.76, 1407.99),
    ((-0.031076, -0.097454), (0.002612, -0.015737, 0.999873), 2832.66, 2855.88, 1968.03, 1452.81),
    ((-0.031321, -0.113729), (-0.017094, 0.068653, 0.997494), 2825.15, 2850.61, 2061.85, 1458.56),
    ((-0.032651, -0.100247), (0.022480, 0.032909, 0.999206), 2832.89, 2857.55, 2027.31, 1492.53),
    ((-0.030791, -0.097424), (0.008205, -0.003560, 0.999960), 2827.30, 2852.24, 1993.26, 1443.37),
    ((-0.030248, -0.099551), (0.034429, 0.085700, 0.995726), 2832.89, 2857.55, 2027.31, 1492.53),
]  # fmt: skip

# (dive_id, x_p5, y_p5, x_p95, y_p95) over that dive's completed labels.
PROD_DIVE_LASER_LOCI = [
    (424, 1792, 1216, 1945, 1380),  # leftmost observed rig
    (262, 1795, 1064, 1872, 1308),
    (253, 2202, 1397, 2328, 1570),  # rightmost -- an uncalibrated rig
    (397, 2196, 1158, 2268, 1364),  # ditto
    (468, 2205, 1431, 2247, 1540),  # ditto
    (437, 2041, 543, 2303, 1425),  # highest; the V-Slate 7 dive
    (446, 1965, 1313, 2110, 1763),  # lowest
]

# Widest per-dive median over all 262 dives -- a median cannot be a mislabel.
PROD_DIVE_MEDIAN_EXTREMES = {"x_min": 1768, "x_max": 2367, "y_min": 1069, "y_max": 1616}

_FRAME_W, _FRAME_H = 4014, 3016


def _polygon_area(poly) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _project_ray(calibration, depths):
    """Pixels swept by one rig's laser ray across `depths` (origin z is 0)."""
    (ox, oy), axis, fx, fy, cx, cy = calibration
    norm = math.sqrt(sum(a * a for a in axis))
    ax, ay, az = (a / norm for a in axis)
    for depth in depths:
        t = depth / az
        x, y, z = ox + t * ax, oy + t * ay, t * az
        yield fx * x / z + cx, fy * y / z + cy


def _depths(n=500):
    lo, hi = WORKING_DEPTH_RANGE_M
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


@pytest.mark.parametrize("calibration", PROD_CALIBRATIONS)
def test_region_contains_every_calibrated_laser_ray(calibration):
    outside = [
        (u, v)
        for u, v in _project_ray(calibration, _depths())
        if not point_in_laser_region(u, v)
    ]
    assert not outside, f"laser ray leaves the region at {outside[:3]}"


@pytest.mark.parametrize("locus", PROD_DIVE_LASER_LOCI)
def test_region_contains_every_observed_dive_locus(locus):
    """All four corners, not just the diagonal: the region is not axis-aligned."""
    dive_id, x1, y1, x2, y2 = locus
    corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    assert all(
        point_in_laser_region(x, y) for x, y in corners
    ), f"dive {dive_id} lasers span x {x1}..{x2} y {y1}..{y2}, outside the region"


def test_region_clears_every_per_dive_median_with_margin():
    x1, y1, x2, y2 = DEFAULT_LASER_BBOX
    e = PROD_DIVE_MEDIAN_EXTREMES
    margins = {
        "left": e["x_min"] - x1,
        "right": x2 - e["x_max"],
        "top": e["y_min"] - y1,
        "bottom": y2 - e["y_max"],
    }
    assert min(margins.values()) >= 100, f"insufficient margin: {margins}"


def test_region_is_a_hint_not_the_whole_frame():
    x1, y1, x2, y2 = DEFAULT_LASER_BBOX
    assert 0 <= x1 < x2 <= _FRAME_W and 0 <= y1 < y2 <= _FRAME_H
    assert _polygon_area(LASER_REGION_POLYGON) / (_FRAME_W * _FRAME_H) < 0.15


def test_region_is_convex_and_wound_consistently():
    poly = LASER_REGION_POLYGON
    assert len(poly) >= 4
    signs = set()
    for i in range(len(poly)):
        (ax, ay), (bx, by), (cx, cy) = (
            poly[i],
            poly[(i + 1) % len(poly)],
            poly[(i + 2) % len(poly)],
        )
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        signs.add(cross > 0)
        assert cross != 0
    assert len(signs) == 1, "region is not convex"


def test_bbox_is_the_regions_bounding_box():
    xs = [v[0] for v in LASER_REGION_POLYGON]
    ys = [v[1] for v in LASER_REGION_POLYGON]
    assert DEFAULT_LASER_BBOX == [min(xs), min(ys), max(xs), max(ys)]


# --- the predicate itself ---------------------------------------------------


def _centre():
    n = len(LASER_REGION_POLYGON)
    return (
        sum(v[0] for v in LASER_REGION_POLYGON) / n,
        sum(v[1] for v in LASER_REGION_POLYGON) / n,
    )


def test_centre_of_the_region_is_inside():
    assert point_in_laser_region(*_centre())


@pytest.mark.parametrize("vertex", LASER_REGION_POLYGON)
def test_vertices_are_inside(vertex):
    assert point_in_laser_region(float(vertex[0]), float(vertex[1]))


def test_edge_midpoints_are_inside():
    for i, start in enumerate(LASER_REGION_POLYGON):
        end = LASER_REGION_POLYGON[(i + 1) % len(LASER_REGION_POLYGON)]
        assert point_in_laser_region(
            (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        ), f"edge {i} midpoint rejected"


@pytest.mark.parametrize(
    "x,y,why",
    [
        (0, 0, "frame origin"),
        (4013, 3015, "opposite frame corner"),
        (1600, 1800, "inside the bbox, but in the corner the polygon cuts"),
        (2450, 1750, "ditto, bottom-right"),
        (1590, 1300, "just left of the region's left edge"),
        (1580, 1905, "bbox corner the polygon cuts off (bottom-left)"),
        (2470, 395, "bbox corner the polygon cuts off (top-right)"),
        (2217, 2088, "the recurring specular-reflection artifact"),
    ],
)
def test_points_outside_are_rejected(x, y, why):
    assert not point_in_laser_region(float(x), float(y)), why


def test_the_cut_corners_are_what_a_rectangle_would_have_wrongly_accepted():
    x1, y1, x2, y2 = DEFAULT_LASER_BBOX
    corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    assert not any(point_in_laser_region(float(x), float(y)) for x, y in corners)


def test_explicit_region_argument_overrides_the_default():
    square = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert point_in_laser_region(5, 5, square)
    assert not point_in_laser_region(50, 50, square)


@pytest.mark.parametrize("degenerate", [[], [[0, 0]], [[0, 0], [1, 1]]])
def test_a_degenerate_region_admits_nothing(degenerate):
    """Fails closed: accepting everything would disable the gate silently."""
    assert not point_in_laser_region(5, 5, degenerate)


def test_winding_order_does_not_matter():
    reversed_poly = list(reversed(LASER_REGION_POLYGON))
    assert point_in_laser_region(*_centre(), reversed_poly)
    assert not point_in_laser_region(0, 0, reversed_poly)
