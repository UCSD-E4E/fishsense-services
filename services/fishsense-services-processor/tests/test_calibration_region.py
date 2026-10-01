"""The convex same-side test the checkerboard gate asks "was the dot on the
board?" with.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
test_laser_region.py: the tests of `point_in_laser_region` with an explicit
region, which is the only way the calibration path calls it (the region is the
detected board's hull). v1's `LASER_REGION_POLYGON` default and the corpus
tests behind it belong to the laser slice; here the region is required.
"""

from __future__ import annotations

import pytest

from fishsense_services_contracts.laser_region import point_in_laser_region

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def test_a_point_inside_is_admitted_and_one_outside_is_not():
    assert point_in_laser_region(5, 5, SQUARE)
    assert not point_in_laser_region(50, 50, SQUARE)


@pytest.mark.parametrize("vertex", SQUARE)
def test_vertices_and_edges_are_inside(vertex):
    """Edges and corners count as inside -- a point exactly on an edge is where
    a ray-casting test gets it wrong."""
    assert point_in_laser_region(float(vertex[0]), float(vertex[1]), SQUARE)
    assert point_in_laser_region(5.0, 0.0, SQUARE)


@pytest.mark.parametrize("degenerate", [[], [[0, 0]], [[0, 0], [1, 1]]])
def test_a_degenerate_region_admits_nothing(degenerate):
    """Fails closed: accepting everything would disable the gate with no
    signal."""
    assert not point_in_laser_region(5, 5, degenerate)


def test_winding_order_does_not_matter():
    reversed_square = list(reversed(SQUARE))
    assert point_in_laser_region(5, 5, reversed_square)
    assert not point_in_laser_region(-1, 5, reversed_square)
