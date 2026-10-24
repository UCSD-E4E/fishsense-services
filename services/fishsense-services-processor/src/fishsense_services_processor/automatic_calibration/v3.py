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

from dataclasses import dataclass, field, fields
from typing import Callable, Optional, Sequence

import numpy as np
from fishsense_core import size_constancy as sc
from fishsense_core.line_fit import consensus_line

__all__ = [
    "CHECK_MEDIAN_PX",
    "CHECK_MIN_DOTS",
    "DOT_SIGMA_PX",
    "SCALE_SD_MAX",
    "SIZE_SIGMA",
    "TRIM_SIGMAS",
    "GROSS_PX",
    "MIN_FRAMES",
    "OFF_LINE_PX",
    "RIVAL_SHARE",
    "Check",
    "LabelFreeFit",
    "V3Fit",
    "beam_line",
    "calibrate_v3",
    "check",
    "fit_known_sizes",
    "gross_screen",
    "refused",
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
#: The known-sizes fit's noise model (2026-10-23, 356 labelled slate frames):
#: automatic slate dots sit a median 0.72 px from human ones along the beam,
#: a normal sigma of ~1.1 px; SAM's slate size scatters 0.3% about the human
#: scale (rounded up).
DOT_SIGMA_PX = 1.1
SIZE_SIGMA = 0.005
#: A frame is trimmed when its deleted residual exceeds this many sigmas of
#: what that noise explains at its own distance from the vanishing point.
TRIM_SIGMAS = 4.0
#: The length scale's predicted SD a fit may have: ~ +-3% at 2 sigma, the
#: owner's measurement budget. Provisional: with a ~10 cm laser offset the dot
#: sits only ~300/Z px from the vanishing point, so 1 px is ~0.3% of range a
#: frame and typical slate geometries predict 1.2-2.7% -- the threshold is to
#: be set from the replay's draws (predicted against measured error), which
#: run with no gate.
SCALE_SD_MAX = 0.015
SCALE_DRAWS = 200


def _xy(dots) -> np.ndarray:
    return np.asarray(dots, float).reshape(-1, 2)


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
    through its inliers. Fewer than two dots: all kept, no line.

    The search is fishsense-core's `line_fit.consensus_line` (core #100),
    which was ported from this function and reproduces it bit for bit; its
    re-scored ``inliers`` are the dots kept."""
    xy = _xy(slate_xy)
    fit = consensus_line(xy, tol_px=tol) if len(xy) >= 2 else None
    if fit is None:  # under two dots, or every dot at one pixel: no direction
        return list(range(len(xy))), None
    return [int(i) for i in np.flatnonzero(fit.inliers)], np.array(
        [fit.a, fit.b, fit.c]
    )


def beam_line(camera_matrix, position, axis) -> np.ndarray:
    """The beam's image: ``(a, b, c)``, unit normal, through its projections
    at 0.5 m and 20 m."""
    k = np.asarray(camera_matrix, float)
    o, a = np.asarray(position, float), np.asarray(axis, float)
    pts = [k @ (o + ((z - o[2]) / a[2]) * a) for z in (0.5, 20.0)]
    line = np.cross(pts[0], pts[1])
    return line / np.hypot(line[0], line[1])


@dataclass(frozen=True)
class LabelFreeFit(sc.LabelFreeFit):
    """fishsense-core's `size_constancy.LabelFreeFit`, plus `scale_sd`, which
    v3 sets and core does not carry (core #97 left services-only fields to
    services): the length scale's predicted SD from dot and size noise
    (calibration v3 on SAM sizes, 2026-10-23); None where not predicted."""

    scale_sd: Optional[float] = None

    @classmethod
    def of(cls, fit: sc.LabelFreeFit) -> "LabelFreeFit":
        """`fit` as this type (unchanged if it already is one)."""
        if isinstance(fit, cls):
            return fit
        return cls(**{f.name: getattr(fit, f.name) for f in fields(sc.LabelFreeFit)})


def refused(reason: str, *, o_mag_m: float = sc.O_DESIGN_M, **seen) -> LabelFreeFit:
    """A refused fit, with what it saw: every field not in `seen` empty."""
    values = {
        "laser_position": None, "laser_axis": None, "vanishing_px": None,
        "line_direction": None, "line_offset_px": None, "used": [],
        "frames_used": 0, "pairs": 0, "size_ratio": None, "se_px": None,
        "pair_residual_sd": None, "o_mag_m": o_mag_m,
    }  # fmt: skip
    values.update(seen)
    return LabelFreeFit(outcome="refused", refusal_reason=reason, **values)


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
    fit: Optional[LabelFreeFit]
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
    fit: Callable[[Sequence[int]], LabelFreeFit],
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


def fit_known_sizes(
    dots,
    sizes,
    camera_matrix,
    *,
    min_frames: int = MIN_FRAMES,
    min_size_ratio: float = sc.MIN_SIZE_RATIO,
    rng: np.random.Generator | None = None,
    scale_sd_max: float = SCALE_SD_MAX,
) -> LabelFreeFit:
    """The size-constancy fit on sizes measured per frame -- the slate's SAM
    mask, sqrt(area) (strategy 2, owner 2026-10-22) -- instead of solved from
    SIFT pair ratios: no pair registration, so no ``too_few_matched_pairs``.
    A frame with no size (no mask under its dot) is left out. Otherwise
    `fit_label_free`'s steps: the direction through the dots, the
    deleted-residual trim, the size spread, the bracket, the bootstrap SE.
    Sizes in any units: only their ratios count.

    2026-10-23: the trim is noise-aware (`_noise_trim`, not the fixed
    `FRAME_RESIDUAL_MAX`), and a fit whose predicted length-scale SD
    (`scale_sd`) exceeds `SCALE_SD_MAX` is refused (``uncertain_fit``): on
    the controls, automatic dots a few px off along the line made 20-27%
    errors where the slates were far."""
    xy = _xy(dots)
    s_all = np.array([np.nan if v is None else float(v) for v in sizes])
    used = np.flatnonzero(np.isfinite(s_all) & (s_all > 0))
    seen = dict(used=[int(i) for i in used], frames_used=len(used), pairs=0,
                pair_residual_sd=None)  # fmt: skip
    if len(used) < min_frames:
        return refused("too_few_frames", **seen)
    u = sc.line_direction(None, xy[used])
    t = xy @ u
    s = s_all
    seen["size_ratio"] = float(s[used].max() / s[used].min())
    if seen["size_ratio"] < min_size_ratio:
        return refused("too_little_range_spread", **seen)
    pinned = sc._pinned(
        t[used], sc.fit_vanishing_position(t[used], s[used])
    )  # pylint: disable=protected-access
    keep = _noise_trim(t, s, used)
    seen.update(used=[int(i) for i in keep], frames_used=len(keep))
    if len(keep) < min_frames:
        return refused("unconstrained" if pinned else "too_few_frames", **seen)
    seen["size_ratio"] = float(s[keep].max() / s[keep].min())
    if seen["size_ratio"] < min_size_ratio:
        return refused("too_little_range_spread", **seen)
    tk, sk = t[keep], s[keep]
    tv = sc.fit_vanishing_position(tk, sk)
    offset = float(np.median(xy[keep] @ np.array([-u[1], u[0]])))
    seen.update(vanishing_px=tv, line_direction=[float(c) for c in u],
                line_offset_px=offset)  # fmt: skip
    if sc._pinned(tk, tv):  # pylint: disable=protected-access
        return refused("unconstrained", **seen)
    sd = scale_sd(tk, sk, u, offset, camera_matrix)
    seen["scale_sd"] = sd
    if sd > scale_sd_max:
        return refused("uncertain_fit", **seen)
    origin, axis = sc.beam_from_vanishing(tv, u, offset, camera_matrix)
    return LabelFreeFit(
        outcome="accepted", refusal_reason=None,
        laser_position=[float(c) for c in origin], laser_axis=[float(c) for c in axis],
        se_px=sc.bootstrap_se(tk, sk, rng=rng), o_mag_m=sc.O_DESIGN_M, **seen,
    )  # fmt: skip


def _ranges_on(camera_matrix, position, axis, pixels) -> np.ndarray:
    """The range (z) the beam gives each pixel: the beam's point nearest the
    pixel's ray."""
    ki = np.linalg.inv(np.asarray(camera_matrix, float))
    o, a = np.asarray(position, float), np.asarray(axis, float)
    out = []
    for x, y in pixels:
        r = ki @ [x, y, 1.0]
        r /= np.linalg.norm(r)
        m = np.array([[a @ a, -a @ r], [a @ r, -r @ r]])
        lam, _ = np.linalg.solve(m, np.array([-(o @ a), -(o @ r)]))
        out.append((o + lam * a)[2])
    return np.array(out)


def scale_sd(t, s, u, offset, camera_matrix, *, draws: int = SCALE_DRAWS,
             rng: np.random.Generator | None = None) -> float:  # fmt: skip
    """The length scale's SD under the noise model: the fit re-run with each
    dot moved along the line by N(0, DOT_SIGMA_PX) and each size by
    N(0, SIZE_SIGMA); each beam's ranges at the nominal beam's pixels for
    0.5-5 m, against those ranges. Lengths scale with range, so this is the
    length error a fit like this one carries. The line's direction is held
    (a sideways error is the check's to see)."""
    rng = rng if rng is not None else np.random.default_rng(0)
    t, s = np.asarray(t, float), np.asarray(s, float)
    tv = sc.fit_vanishing_position(t, s)
    o, a = sc.beam_from_vanishing(tv, u, offset, camera_matrix)
    k = np.asarray(camera_matrix, float)
    zs = np.linspace(0.5, 5.0, 10)
    pix = []
    for z in zs:
        p = k @ (o + ((z - o[2]) / a[2]) * a)
        pix.append(p[:2] / p[2])
    ratios = []
    for _ in range(draws):
        t2 = t + rng.normal(0.0, DOT_SIGMA_PX, len(t))
        s2 = s * np.exp(rng.normal(0.0, SIZE_SIGMA, len(s)))
        o2, a2 = sc.beam_from_vanishing(sc.fit_vanishing_position(t2, s2), u, offset,
                                        camera_matrix)  # fmt: skip
        ratios.append(float(np.median(_ranges_on(camera_matrix, o2, a2, pix) / zs)))
    return float(np.std(ratios))


def _noise_trim(t: np.ndarray, s: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Drop, one at a time, the frame whose deleted residual is furthest past
    `TRIM_SIGMAS` sigmas of what dot and size noise explain at its distance
    from the vanishing point (the fit of the others)."""
    keep = np.asarray(keep)
    while len(keep) > 2:
        z = np.empty(len(keep))
        for j, i in enumerate(keep):
            rest = keep[keep != i]
            tv = sc.fit_vanishing_position(t[rest], s[rest])
            c = np.median(np.log(s[rest]) - np.log(t[rest] - tv))
            gap = t[i] - tv
            if gap <= 0:
                z[j] = np.inf
                continue
            sigma = np.hypot(DOT_SIGMA_PX / gap, SIZE_SIGMA)
            z[j] = abs(np.log(s[i]) - np.log(gap) - c) / sigma
        worst = int(np.argmax(z))
        if z[worst] <= TRIM_SIGMAS:
            break
        keep = np.delete(keep, worst)
    return keep
