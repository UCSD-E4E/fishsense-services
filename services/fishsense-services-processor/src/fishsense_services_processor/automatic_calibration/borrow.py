"""The checked borrow's check (v27; the owner, 2026-10-29): does a neighbor's
beam read the borrower's own slates at their depth?

New in v2. An aim with no calibration of its own (after a beam move, or a
mission whose own fit was refused) may borrow the beam of another mission on
its device that day; the API picks the one that runs along the aim's own dot
line (`fishsense_services_api.calibration_borrow`). The line alone does not
catch a bad borrow: a beam can tilt within the plane of its image line,
changing the scale without moving the line. On 33 missions with their own
slate-pose fit hidden (prod, 2026-10-29), the line-checked borrow read a
median 1.2-3.2% from the mission's own fit at 0.75-2 m but 5 of the 33 were
8-19% off at 2 m, and neither the line offset (Spearman 0.05) nor the time
between the missions (0.19) predicted which.

So the borrower's own slate frames, posed on the GPU (`calibration_v4`), are
read through the beam: each posed point's camera ray (through its dot) meets
the beam, or passes closest to it, at the beam's depth for that dot, against
the pose's.
Each slate hold (`pose._bursts`) gives its median ratio, the check is the
median over holds -- a hold is one slate at one range, so one misposed slate
moves one vote -- and a beam reading the slates more than `DEPTH_TOLERANCE`
off is refused (``slates_disagree``). On the same 33, the 5% check kept 24,
all within 7.8% of the own fit at 2 m, and dropped all 5 bad borrows.

The check measures the borrowed beam against poses, not against a known
length: a pose's own error (its range from the printed pattern) is in it,
and a beam right at the slates' ranges may still be off beyond them.
"""

from __future__ import annotations

import uuid

import numpy as np

from fishsense_services_contracts.calibration_borrow import (
    BorrowCheckInput,
    BorrowCheckReport,
)
from fishsense_services_processor.automatic_calibration import pose

__all__ = ["DEPTH_TOLERANCE", "MIN_POSED_POINTS", "check_borrowed_beam"]

#: The check's bound on |median over holds of (beam depth / pose depth - 1)|.
DEPTH_TOLERANCE = 0.05
#: Fewer posed points are no check: the smallest the 33-mission control kept
#: rested on 2 (2026-10-29).
MIN_POSED_POINTS = 2


def _depth_on_beam(origin, axis, point) -> float:
    """The depth where the camera ray through `point` passes closest to the
    beam."""
    ray = point / np.linalg.norm(point)
    a = np.array([[axis @ axis, -axis @ ray], [axis @ ray, -ray @ ray]])
    b = np.array([-(origin @ axis), -(origin @ ray)])
    along, _ = np.linalg.solve(a, b)
    return float((origin + along * axis)[2])


def check_borrowed_beam(request: BorrowCheckInput) -> BorrowCheckReport:
    base = {"mission_id": request.mission_id, "lender_id": request.lender_id}
    posed = [(c, p) for c, p in request.pose_points.items() if p is not None]
    if len(posed) < MIN_POSED_POINTS:
        return BorrowCheckReport(**base, outcome="refused",
                                 refusal_reason="too_few_posed",
                                 points=len(posed))  # fmt: skip
    origin = np.asarray(request.laser_position, float)
    axis = np.asarray(request.laser_axis, float)
    axis = axis / np.linalg.norm(axis)
    points = np.array([p for _, p in posed], float)
    ratios = np.array([_depth_on_beam(origin, axis, p) / p[2] - 1 for p in points])
    holds = pose._bursts(points)  # pylint: disable=protected-access
    per_hold = [float(np.median(ratios[holds == h])) for h in np.unique(holds)]
    ratio = float(np.median(per_hold))
    within = abs(ratio) <= DEPTH_TOLERANCE
    return BorrowCheckReport(
        **base, outcome="accepted" if within else "refused",
        refusal_reason=None if within else "slates_disagree",
        depth_ratio=ratio, holds=len(per_hold), points=len(posed),
        capture_ids=[uuid.UUID(c) for c, _ in posed],
    )  # fmt: skip
