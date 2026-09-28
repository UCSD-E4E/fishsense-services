"""Whether a point lies in a convex region: the test the checkerboard gate asks
"was the dot on the board?" with.

Ported from fishsense-lite@77e8f8e5
libs/fishsense-shared/src/fishsense_shared/laser_region.py
(`point_in_laser_region`), the function only. v1's `LASER_REGION_POLYGON`
default, the polygon the laser stages gate predictions on, is the laser
slice's to port; the calibration path always passes its own region (the
detected board's hull), so here the region is required.

Convex, so a point is inside iff it is on the same side of every directed
edge -- no ray casting, no winding number, and no special case for a point
sitting exactly on an edge, which a ray-casting test decides by rounding.
"""

from __future__ import annotations

from typing import Sequence

__all__ = ["point_in_laser_region"]


def point_in_laser_region(
    x: float, y: float, region: Sequence[Sequence[float]]
) -> bool:
    """Whether `(x, y)` lies inside the (convex) `region`, edges included.

    A degenerate region (fewer than 3 vertices) admits nothing rather than
    everything: this gates whether an observation is believed, and failing
    open would silently disable the gate.
    """
    poly = [tuple(v) for v in region]
    if len(poly) < 3:
        return False

    positive = negative = False
    for index, (ax, ay) in enumerate(poly):
        bx, by = poly[(index + 1) % len(poly)]
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if cross > 0:
            positive = True
        elif cross < 0:
            negative = True
        if positive and negative:
            return False
    return True
