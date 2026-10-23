"""Label-free calibration, version 3: the slates fit the beam, the rest of the
dive checks it (owner, 2026-10-22).

Version 2 fitted the dive line through every automatic dot and kept only the
slate dots on it, with 8 needed. The slates are a small part of a dive, so
wrong dots elsewhere can outvote them, and many dives have fewer than 8.
Version 3, the owner's design:

1. **Gross screen** (`gross_screen`): the dive line through every automatic dot
   drops only slate dots far off it (`GROSS_PX`) -- specks and reflections
   nowhere near the beam. It decides nothing finer.
2. **Slate fit**: the slate dots that remain are judged against each other
   (`slate_consensus`: the line most of them agree on, within `OFF_LINE_PX`);
   the fit (`size_constancy.fit_label_free`) takes its direction from them,
   with at least `MIN_FRAMES` (3: enough to tell whether they agree; from 4
   its trim can tell which one does not). A rival line among the rest as big
   as `max(MIN_FRAMES, RIVAL_SHARE * kept)` is a laser and its reflection that
   can't be told apart: ``ambiguous_line``.
3. **Check** (`check`): the fitted beam, projected into the image, against
   every non-slate dot -- dots the fit never saw. If at least `CHECK_MIN_DOTS`
   are there and their median distance exceeds `CHECK_MEDIAN_PX`, the fit
   disagrees with its own dive and is refused (``fit_disagrees_with_mission``).
   (The share within `OFF_LINE_PX` is recorded, not a second test: under half
   within 10 px *is* a median over 10 px.) Accepted, a
   frame whose dot is off the beam is the frame's problem, not the fit's (the
   measure step's to refuse).

**What the check cannot see** (2026-10-22, the 57 accepted v2 fits): the
projected line moves with a sideways error in the beam (0.25 deg moves the
non-slate dots' median distance ~12 px) but not at all with an error along
it -- the vanishing position, i.e. the range scale, which a 0.5 deg error
moves 11-14% while leaving 0-3% of ranges implausible. That part rests on the
slate fit alone (its size spread, standard error and trim), which is what the
small-n draws measure.

Thresholds from the same 57 fits: non-slate automatic dots sit a median
2.4 px off a good fit (worst 9.0), 97% within 10 px (p10 73%, worst 52%);
human dots 1.7 px.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable, Optional, Sequence

import numpy as np

from fishsense_services_processor.automatic_results import size_constancy as sc

__all__ = [
    "CHECK_MEDIAN_PX",
    "CHECK_MIN_DOTS",
    "GROSS_PX",
    "MIN_FRAMES",
    "OFF_LINE_PX",
    "RIVAL_SHARE",
    "Check",
    "V3Fit",
    "beam_line",
    "calibrate_v3",
    "check",
    "gross_screen",
    "slate_consensus",
]

#: Step 1's margin from the dive line: five times the 10 px that holds 97% of
#: non-slate dots on a good fit.
GROSS_PX = 50.0
#: The slates' and the check's tolerance (production's off-line gate).
OFF_LINE_PX = 10.0
MIN_FRAMES = 3
RIVAL_SHARE = 0.25
#: The check decides only with this many non-slate dots.
CHECK_MIN_DOTS = 10
CHECK_MEDIAN_PX = 10.0


def _xy(dots) -> np.ndarray:
    return np.asarray(dots, float).reshape(-1, 2)


def _line_through(xy: np.ndarray) -> np.ndarray:
    """``(a, b, c)``, unit normal, of the total-least-squares line."""
    m = xy.mean(axis=0)
    u = np.linalg.svd(xy - m)[2][0]
    n = np.array([-u[1], u[0]])
    return np.array([n[0], n[1], -n @ m])


def _distances(xy: np.ndarray, line) -> np.ndarray:
    a, b, c = line
    return np.abs(xy @ np.array([a, b]) + c) / np.hypot(a, b)


def gross_screen(slate_xy, dive_line, gross_px: float = GROSS_PX) -> list[int]:
    """Indices of slate dots within `gross_px` of the dive line; all of them
    with no dive line."""
    xy = _xy(slate_xy)
    if dive_line is None:
        return list(range(len(xy)))
    return [int(i) for i in np.flatnonzero(_distances(xy, dive_line) <= gross_px)]


def slate_consensus(slate_xy, tol: float = OFF_LINE_PX):
    """``(kept indices, line)``: the line the most dots agree on within `tol`
    -- every pair's line tried, ties to the smaller summed distance -- refitted
    through its inliers. Fewer than two dots: all kept, no line."""
    xy = _xy(slate_xy)
    if len(xy) < 2:
        return list(range(len(xy))), None
    best = None
    for i, j in combinations(range(len(xy)), 2):
        d = xy[j] - xy[i]
        if np.hypot(*d) < 1.0:
            continue
        line = _line_through(xy[[i, j]])
        dist = _distances(xy, line)
        inl = dist <= tol
        key = (int(inl.sum()), -float(dist[inl].sum()))
        if best is None or key > best[0]:
            best = (key, inl)
    if best is None:  # every dot at one pixel: they agree, on no direction
        return list(range(len(xy))), None
    inl = best[1]
    line = _line_through(xy[inl])
    inl = _distances(xy, line) <= tol
    return [int(i) for i in np.flatnonzero(inl)], line


def beam_line(camera_matrix, position, axis) -> np.ndarray:
    """The beam's image: ``(a, b, c)``, unit normal, through its projections
    at 0.5 m and 20 m."""
    k = np.asarray(camera_matrix, float)
    o, a = np.asarray(position, float), np.asarray(axis, float)
    pts = [k @ (o + ((z - o[2]) / a[2]) * a) for z in (0.5, 20.0)]
    line = np.cross(pts[0], pts[1])
    return line / np.hypot(line[0], line[1])


@dataclass(frozen=True)
class Check:
    dots: int
    median_px: Optional[float]
    within_share: Optional[float]

    @property
    def decides(self) -> bool:
        return self.dots >= CHECK_MIN_DOTS

    @property
    def disagrees(self) -> bool:
        return self.decides and self.median_px > CHECK_MEDIAN_PX


def check(camera_matrix, position, axis, other_xy) -> Check:
    """The non-slate dots against the fitted beam's image."""
    xy = _xy(other_xy)
    if not len(xy):
        return Check(0, None, None)
    d = _distances(xy, beam_line(camera_matrix, position, axis))
    return Check(len(xy), float(np.median(d)), float(np.mean(d <= OFF_LINE_PX)))


@dataclass(frozen=True)
class V3Fit:
    outcome: str
    refusal_reason: Optional[str]
    #: The size-constancy fit, when one ran (its indices are into `kept`).
    fit: Optional[sc.LabelFreeFit]
    #: Indices (into the slate dots given) the fit was given.
    kept: list[int] = field(default_factory=list)
    gross_dropped: int = 0
    consensus_dropped: int = 0
    rival: int = 0
    check: Check = Check(0, None, None)


def calibrate_v3(
    *,
    slate_xy,
    dive_line,
    other_xy,
    camera_matrix,
    fit: Callable[[Sequence[int]], sc.LabelFreeFit],
) -> V3Fit:
    """Steps 1-3 for one dive. `fit(indices)` runs the size-constancy fit on
    those slate frames (direction through their own dots, at least
    `MIN_FRAMES`): the activity's, over features it extracted once."""
    xy = _xy(slate_xy)
    screened = gross_screen(xy, dive_line)
    gross_dropped = len(xy) - len(screened)
    kept_local, _ = slate_consensus(xy[screened])
    kept = [screened[i] for i in kept_local]
    off = [i for i in screened if i not in set(kept)]
    rival = len(slate_consensus(xy[off])[0]) if len(off) >= 2 else len(off)
    base = dict(kept=kept, gross_dropped=gross_dropped,
                consensus_dropped=len(screened) - len(kept), rival=rival)  # fmt: skip
    if len(kept) < MIN_FRAMES:
        return V3Fit("refused", "too_few_frames", None, **base)
    if rival >= max(MIN_FRAMES, RIVAL_SHARE * len(kept)):
        return V3Fit("refused", "ambiguous_line", None, **base)
    result = fit(kept)
    if result.outcome != "accepted":
        return V3Fit("refused", result.refusal_reason, result, **base)
    seen = check(camera_matrix, result.laser_position, result.laser_axis, other_xy)
    if seen.disagrees:
        return V3Fit(
            "refused", "fit_disagrees_with_mission", result, check=seen, **base
        )
    return V3Fit("accepted", None, result, check=seen, **base)
