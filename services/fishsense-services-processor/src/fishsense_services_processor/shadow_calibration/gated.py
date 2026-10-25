"""The shadow calibration's gated variant: the committed gate and refusals.

docs/shadow-size-constancy.md §13 commits these values (author, 2026-10-21,
before any backfill; not to be adjusted after seeing results). The `port`
variant is cscw's script exactly (`fit.port_fit`); this adds, around it:

- **the detector-dot consistency gate** (`detector_gate`, §5.3), for the
  automatic track's dots: presence confidence; distance from the detector line
  (the port's `prediction_lines()`: RANSAC, 10 px, 500 iterations,
  `default_rng(0)`, TLS through the inliers); and an ambiguous line, when the
  off-line slate dots form a line of their own (session 77's reflection);
- **range consistency** (`trim`): a frame whose deleted residual -- its log
  size against the fit made without it -- exceeds 0.10 is dropped, one at a
  time. Dive 279's red specks lie on the laser line at the wrong range, so no
  line gate can see them; this can (imwut IN_PLANE_DRIFT.md:346-358);
- **the refusals** (§6), recorded in the committed order. The 0.6 m rule is
  production's own lever-arm gate, `fishsense_core.calibration_checks.
  check_observation_geometry` (services' `calibration.consistency` until
  fishsense-core 4.3.0), on the fitted laser points, not a new rule.

Also recorded, deciding nothing: the lever arm with the mission's own
production offset (author's sensitivity check), and slate length constancy --
the slate's size in metres across range (§8.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

import numpy as np
from fishsense_core import size_constancy as sc
from fishsense_core.calibration_checks import (
    CalibrationUnderdeterminedError,
    check_observation_geometry,
)
from fishsense_core.line_fit import fit_line_pca, ransac_line

from fishsense_services_processor.shadow_calibration.fit import (
    O_DESIGN_M,
    PortFit,
    port_fit,
)

__all__ = [
    "COMMITTED",
    "DetectorGate",
    "Frame",
    "GatedResult",
    "Thresholds",
    "detector_gate",
    "gated_fit",
    "laser_points",
]


@dataclass(frozen=True)
class Thresholds:
    """The committed values (§13). `but` makes a variant for a test."""

    min_confidence: float = 0.5
    off_line_px: float = 10.0
    second_line_min: int = 8
    second_line_share: float = 0.25
    residual_max: float = 0.10
    min_frames: int = 8
    min_size_spread: float = 1.5
    max_se_deg: float = 0.05
    min_median_sam_score: float = 0.5
    max_no_mask_share: float = 0.25
    refusal_order: tuple = (
        "too_few_frames",
        "too_little_size_spread",
        "lever_arm_below_0.6m",
        "se_too_high",
        "sam_scores_low",
        "unconstrained",
        "ambiguous_line",
    )

    def but(self, **changes) -> "Thresholds":
        return replace(self, **changes)


COMMITTED = Thresholds()


@dataclass(frozen=True)
class Frame:
    """One slate frame with its dot (full-res rectified px) and its SAM
    result; `size` is None when SAM kept no mask under the dot."""

    capture_id: Any
    x: float
    y: float
    size: Optional[float]
    sam_score: Optional[float]
    frame_source: str = "label"


@dataclass(frozen=True)
class DetectorGate:
    u: np.ndarray
    offset: float
    line_points: int
    line_of: int
    kept: tuple
    #: capture -> ``confidence_low`` | ``off_line``
    rejected: dict
    second_line: int
    ambiguous_line: bool


def _ransac_line(xy: np.ndarray, tol: float):
    """size_constancy_sam.prediction_lines: RANSAC, then TLS through the
    inliers; ``(u, offset, inliers)``, u oriented to negative y. None when no
    two points are a pixel apart: coincident dots define no line (review of
    #261).

    The search is fishsense-core's `line_fit.ransac_line` (core #100), ported
    from this function: 500 iterations from ``default_rng(0)``, inclusive at
    `tol`, pairs under 1 px apart skipped, refitted by `fit_line_pca` (the
    same SVD as here). The inliers are its ``consensus``, the winning pair's
    line's points, as here. It scores distances as ``|a x + b y + c|`` where
    this scored ``|xy @ n - n @ p_i|``: equal to rounding, so the two can
    disagree only on a dot within rounding of exactly `tol` px off."""
    fit = ransac_line(
        xy, tol_px=tol, max_iters=500, rng=0, inclusive=True,
        min_separation_px=1, refit=fit_line_pca,
    )  # fmt: skip
    if fit is None:
        return None
    # fit_line_pca's normal is the principal direction u turned 90 degrees,
    # (a, b) = (-u_y, u_x), and c = -(a, b) . centroid: so u = (b, -a), and
    # the offset (the centroid along u's normal) is -c, or c once u is
    # turned to negative y.
    u = np.array([fit.b, -fit.a])
    if u[1] < 0:
        return u, -fit.c, fit.consensus
    return -u, fit.c, fit.consensus


def detector_gate(
    *,
    slate_dots: Sequence[tuple],
    mission_dots: np.ndarray,
    thresholds: Thresholds = COMMITTED,
) -> Optional[DetectorGate]:
    """The gate over a mission's detector dots. `slate_dots` are
    ``(capture, x, y, confidence)`` on its slate frames; `mission_dots` its
    every frame's ``(x, y, confidence)``, the detector line's population.
    None when the confident dots define no line (fewer than two, or all at
    one pixel)."""
    th = thresholds
    md = np.asarray(mission_dots, float).reshape(-1, 3)
    confident = md[md[:, 2] >= th.min_confidence][:, :2]
    line = _ransac_line(confident, th.off_line_px) if len(confident) >= 2 else None
    if line is None:
        return None
    u, off, inl = line
    n = np.array([-u[1], u[0]])
    kept, rejected, off_dots = [], {}, []
    for capture, x, y, conf in slate_dots:
        if conf < th.min_confidence:
            rejected[capture] = "confidence_low"
        elif abs(np.array([x, y]) @ n - off) > th.off_line_px:
            rejected[capture] = "off_line"
            off_dots.append((x, y))
        else:
            kept.append(capture)
    second = 0
    if len(off_dots) >= 2:
        second_line = _ransac_line(np.array(off_dots, float), th.off_line_px)
        second = 0 if second_line is None else int(second_line[2].sum())
    ambiguous = second >= max(th.second_line_min, th.second_line_share * len(kept))
    return DetectorGate(
        u=u, offset=off, line_points=int(inl.sum()), line_of=len(confident),
        kept=tuple(kept), rejected=rejected, second_line=second,
        ambiguous_line=bool(ambiguous),
    )  # fmt: skip


def laser_points(xy, K, origin, axis) -> np.ndarray:
    """Each dot's camera-space point on the beam: the beam's point closest to
    the dot's pixel ray."""
    Ki = np.linalg.inv(np.asarray(K, float))
    pts = []
    for x, y in np.asarray(xy, float):
        r = Ki @ [x, y, 1.0]
        r /= np.linalg.norm(r)
        a = np.array([[axis @ axis, -axis @ r], [axis @ r, -r @ r]])
        b = np.array([-(origin @ axis), -(origin @ r)])
        lam, _ = np.linalg.solve(a, b)
        pts.append(origin + lam * axis)
    return np.array(pts)


def trim(t: np.ndarray, s: np.ndarray, residual_max: float) -> np.ndarray:
    """Indices kept: drop, one at a time, the frame whose deleted residual is
    largest while it exceeds `residual_max`. The residual is fishsense-core's
    `size_constancy.deleted_residuals` (core #97), which this module's own
    copy was line for line."""
    keep = np.arange(len(t))
    while len(keep) > 2:
        r = sc.deleted_residuals(t[keep], s[keep])
        worst = int(np.argmax(r))
        if r[worst] <= residual_max:
            break
        keep = np.delete(keep, worst)
    return keep


@dataclass(frozen=True)
class GatedResult:
    outcome: str
    refusal_reason: Optional[str]
    #: Every refusal that applies, in the committed order.
    refusals: tuple
    fit: Optional[PortFit]
    used: tuple
    #: Dropped by range consistency.
    trimmed: tuple
    no_mask: tuple
    lever_m: Optional[float] = None
    lever_m_production_offset: Optional[float] = None
    size_cv_pct: Optional[float] = None
    size_maxdev_pct: Optional[float] = None
    sam_score_min: Optional[float] = None
    sam_score_median: Optional[float] = None
    no_mask_share: float = 0.0
    ranges_m: dict = field(default_factory=dict)


def _pinned(t: np.ndarray, tv: float) -> bool:
    hi = float(np.min(t)) - 5.0
    return tv > hi - 1.0 or tv < hi - 4000.0 + 1.0


def gated_fit(
    frames: Sequence[Frame],
    *,
    u: np.ndarray,
    off: float,
    K: np.ndarray,
    reference_axis: np.ndarray,
    reference_position: Optional[np.ndarray] = None,
    seed: tuple[int, int],
    size_to_full_px: float = 1.0,
    ambiguous_line: bool = False,
    thresholds: Thresholds = COMMITTED,
    o_mag: float = O_DESIGN_M,
) -> GatedResult:
    """The gated fit of one dot source's slate frames. `size_to_full_px`
    converts a size (sqrt-area at the render SAM saw) to full-res px, for the
    slate's size in metres."""
    th = thresholds
    u, K = np.asarray(u, float), np.asarray(K, float)
    sized = [f for f in frames if f.size is not None]
    no_mask = tuple(f.capture_id for f in frames if f.size is None)
    no_mask_share = len(no_mask) / len(frames) if frames else 1.0
    base = dict(no_mask=no_mask, no_mask_share=no_mask_share)
    trimmed: tuple = ()
    if len(sized) >= 3:
        xy = np.array([[f.x, f.y] for f in sized])
        s = np.array([f.size for f in sized])
        keep = trim(xy @ u, s, th.residual_max)
        trimmed = tuple(
            sized[i].capture_id for i in range(len(sized)) if i not in set(keep)
        )
        sized = [sized[i] for i in keep]
    reasons = []
    if len(sized) < th.min_frames:
        reasons.append("too_few_frames")
    if ambiguous_line:
        reasons.append("ambiguous_line")
    if len(sized) < 3:
        return GatedResult(
            outcome="refused",
            refusal_reason=_first(reasons, th),
            refusals=_ordered(reasons, th),
            fit=None, used=(), trimmed=trimmed, **base,
        )  # fmt: skip
    xy = np.array([[f.x, f.y] for f in sized])
    s = np.array([f.size for f in sized])
    fit = port_fit(xy, s, u, off, K, reference_axis, seed=seed, o_mag=o_mag)
    pts = laser_points(xy, K, fit.origin, fit.axis)
    z = pts[:, 2]
    lever = float(z.max() - z.min())
    try:
        check_observation_geometry(pts)
    except CalibrationUnderdeterminedError:
        reasons.append("lever_arm_below_0.6m")
    lever_prod = None
    if reference_position is not None:
        o_prod = float(np.linalg.norm(np.asarray(reference_position, float)[:2]))
        origin_p, _ = sc.beam_from_vanishing(fit.tv, u, off, K, o_prod)
        zp = laser_points(xy, K, origin_p, fit.axis)[:, 2]
        lever_prod = float(zp.max() - zp.min())
    scores = [f.sam_score for f in sized if f.sam_score is not None]
    if fit.size_spread < th.min_size_spread:
        reasons.append("too_little_size_spread")
    if fit.se_deg > th.max_se_deg:
        reasons.append("se_too_high")
    median_score = float(np.median(scores)) if scores else 0.0
    if median_score < th.min_median_sam_score or no_mask_share > th.max_no_mask_share:
        reasons.append("sam_scores_low")
    if _pinned(xy @ u, fit.tv):
        reasons.append("unconstrained")
    size_m = s * size_to_full_px / K[0][0] * z
    return GatedResult(
        outcome="refused" if reasons else "accepted",
        refusal_reason=_first(reasons, th),
        refusals=_ordered(reasons, th),
        fit=fit,
        used=tuple(f.capture_id for f in sized),
        trimmed=trimmed,
        lever_m=lever,
        lever_m_production_offset=lever_prod,
        size_cv_pct=float(100 * np.std(size_m) / np.mean(size_m)),
        size_maxdev_pct=float(100 * np.max(np.abs(size_m / np.median(size_m) - 1))),
        sam_score_min=float(min(scores)) if scores else None,
        sam_score_median=median_score if scores else None,
        ranges_m={f.capture_id: float(zi) for f, zi in zip(sized, z)},
        **base,
    )


def _ordered(reasons, th: Thresholds) -> tuple:
    return tuple(r for r in th.refusal_order if r in set(reasons))


def _first(reasons, th: Thresholds) -> Optional[str]:
    ordered = _ordered(reasons, th)
    return ordered[0] if ordered else None
