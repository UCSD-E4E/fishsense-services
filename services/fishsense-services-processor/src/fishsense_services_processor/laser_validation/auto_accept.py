"""Decide which laser predictions may skip human review.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/
laser_label_validation/auto_accept.py. v2: `dive_id` and `image_id` are the
dive's and the capture's `number` (v1's ids for a migrated row), so the
deterministic audit sample picks the frames v1 picked.

The detector agrees with the labeler exactly 93% of the time (measured over
504 seeded prod predictions, laser-detector-v2): the submitted coordinate is
byte-identical to the prediction. Those reviews cost ~6.4 s each and return no
information. This module decides which of them can be skipped.

The whole design rests on one asymmetry. A laser dot's position is not
independently checkable per frame — that is what killed the slate detector,
whose per-frame ECC gate passed confident false fits — but across a dive it
is, because the dots are collinear: the projection of a laser ray that is
fixed for the whole dive. A diver is swimming, so no feature in the *scene*
persists across a dive; the rig is the only thing that does. So a false
detection has to be wrong in a way that agrees with dozens of other false
detections to get through, and scattered failure cannot manufacture that.

Consequences that shape the code:

* **The dive gate comes first, and it fails closed.** A detector doing badly
  produces predictions that do not agree, no line clears the participation
  bar, and every frame routes to a human. Poor out-of-distribution
  performance shows up as the automation declining to engage rather than as
  bad labels.
* **Two per-frame axes, not one.** Perpendicular distance is blind to a dot
  slid ALONG the laser's epipolar line, which is the direction that moves
  depth most (the same blindness `LaserDepth.residual_m` has). Prod dive 491
  image 132158 sat 1.5 px off the line and 190 px along it, at confidence
  0.9897 — a perpendicular-only gate auto-accepts it.
* **Detector confidence is not used.** It does not separate: 15 of the 22
  moved predictions scored >= 0.99 and 13 scored exactly 1.0000. It is a
  good "is there a dot" signal and a useless "is it the right dot" signal.
  It stays out of the gate deliberately.

Thresholds are measured, not chosen; see `AutoAcceptConfig`.

**Fail-closed is verified out of distribution, on reef.** Every threshold above
was calibrated on pool dives, which is exactly how the slate detector died — so
the gate was replayed against dive 442 (`101624_Alligator0_FSL02`, Alligator
Reef), the one reef dive carrying v2 predictions. It refused the dive:
inlier_fraction 0.404 against the 0.75 bar, `weak_consensus`, 0 of 203 frames
auto-accepted. The refusal is correct, not conservative — only 56.7% of those
predictions land within 10 px of the line the dive's own 358 human labels
define, with p90 at 245 px. The humans are collinear to 2.9 px max; the
detector is what is scattered.

Two things follow, and they are separate. First, the safety property holds on
real out-of-distribution data: the automation declines to engage rather than
producing bad labels, which is the property the ECC gate lacked. Second, the
laser detector itself has a reef problem that has nothing to do with this
module — 56.7% on-line here against ~93% byte-exact agreement with labelers on
pool — so until that improves, auto-accept saves labour in pool-like conditions
and correctly does nothing elsewhere.

Confidence cannot triage it, which is worth knowing before anyone tries: 49% of
the OFF-line predictions on that dive score >= 0.99 (against 66% of the on-line
ones), and a prefilter at any cut from 0.5 to 0.9999 leaves the inlier fraction
between 0.40 and 0.48 — still refused. The detector is confidently wrong about
half the time it is wrong.

Caveat on n: one reef dive is not "reef", and its predicted images are ones
that never received a Label Studio task, a different population from the
labelled ones it is being compared against.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

import numpy as np

from fishsense_core.laser import (
    LABEL_NOISE_MAD_FLOOR_PX,
    LINE_CONFIDENCE_THRESHOLD,
    MAD_TO_SIGMA,
    MIN_POINTS_FOR_LINE,
    fit_dive_line,
)


class DiveIneligibleReason(Enum):
    """Why a dive's predictions may not be auto-accepted at all."""

    TOO_FEW_PREDICTIONS = "too_few_predictions"
    NO_LINE = "no_line"
    WEAK_CONSENSUS = "weak_consensus"
    UNCONFIDENT_LINE = "unconfident_line"
    #: The dive carries predictions from a detector version that is not the
    #: current one. Unlike the others this is not a property of the fit -- it
    #: is decided before fitting, in the activity, because a line drawn across
    #: two different detector behaviours is not a meaningful line. See
    #: `fishsense_shared.laser_predictor`: stage v1 (and every pre-versioning
    #: NULL row) hardcoded the pre-annotation to "Red Laser" instead of reading
    #: the dot, so auto-accepting one writes a possibly-wrong colour into the
    #: corpus with no human in the loop.
    STALE_PREDICTOR = "stale_predictor"


class FrameVerdict(Enum):
    """Per-frame outcome. Only AUTO_ACCEPTED skips a human."""

    AUTO_ACCEPTED = "auto_accepted"
    OFF_LINE = "off_line"
    ALONG_LINE_OUTLIER = "along_line_outlier"
    NO_PREDICTION = "no_prediction"
    AUDIT_SAMPLE = "audit_sample"
    DIVE_INELIGIBLE = "dive_ineligible"


@dataclass(frozen=True)
class AutoAcceptConfig:
    """Gate thresholds. Every default was measured against prod.

    `enabled` is the kill switch, and it is also the dark run. Turned off, the
    gate still fits, still judges, and still records every verdict and margin —
    it just clears nothing, so no frame skips a human. A week disabled
    therefore answers "what would this do to our corpus" exactly, on current
    data, at zero risk; and if something goes wrong later, flipping it off
    stops the behaviour without unwinding a single row. The whole config is
    read from settings on the data-worker, so neither move needs a deploy.

    `min_predictions` / `min_inlier_fraction` are a floor-and-percentage pair
    because each covers the other's blind spot: a floor alone passes a
    1698-frame dive on 40 agreeing points, and a percentage alone passes 5 of
    5. Both must hold.

    `min_inlier_fraction = 0.75` sits in a measured gap. Fitting on
    predictions rather than labels, the eight v2 dives scored 0.842-1.000 and
    the three v1 dives — the only "detector doing badly" evidence in the
    corpus — scored 0.369, 0.413 and 0.679. Do NOT recalibrate this against
    `divelaserline`: those fits run on human labels *after* the validator
    superseded the outliers, so their inlier fraction is ~1.0 by construction
    (p10 = 0.985) and a bar set there is inert.

    `min_predictions = 20` keeps 164 of 226 dives and 95.5% of frames.
    `fishsense_core.laser.MIN_POINTS_FOR_LINE` is 5, fine for the validator —
    whose worst case is discarding a good label — but far too low here, where
    the worst case is keeping a bad one silently. At n=5, three bad points are
    a majority and RANSAC only needs two to hypothesise.

    `max_perpendicular_px = 10.0`: on prod, accepted predictions sat a median
    1 px off the dive line and moved ones 68 px. A 10 px band caught 19 of 22
    moves and 13 of 13 deletions while wrongly flagging 3 of 465 accepted.

    `max_along_line_z = 4.0`: the dive-491 slide scored 5.2 on this measure.
    Tuned against a single real example, which is the weakest-founded number
    here — see `_along_line_z` for what it cannot see.
    """

    enabled: bool = True
    min_predictions: int = 20
    min_inlier_fraction: float = 0.75
    min_line_confidence: float = LINE_CONFIDENCE_THRESHOLD
    max_perpendicular_px: float = 10.0
    max_along_line_z: float = 4.0
    # Share of otherwise-auto-accepted frames still sent to a human. This is
    # not the safety net — the per-dive flag rate is, and it is free. What the
    # sample buys is a comparison set: without it the only frames a human sees
    # are the ones the gate flagged, which is a biased sample that cannot tell
    # you whether the next detector version is better or worse.
    audit_sample_rate: float = 0.10

    def __post_init__(self) -> None:
        if self.min_predictions < MIN_POINTS_FOR_LINE:
            raise ValueError(
                f"min_predictions={self.min_predictions} is below the line "
                f"fit's own MIN_POINTS_FOR_LINE={MIN_POINTS_FOR_LINE}"
            )
        if not 0.0 <= self.min_inlier_fraction <= 1.0:
            raise ValueError("min_inlier_fraction must be in [0, 1]")
        if not 0.0 <= self.audit_sample_rate <= 1.0:
            raise ValueError("audit_sample_rate must be in [0, 1]")
        if self.max_perpendicular_px <= 0 or self.max_along_line_z <= 0:
            raise ValueError("distance thresholds must be positive")


DEFAULT_CONFIG = AutoAcceptConfig()


@dataclass(frozen=True)
class DiveGate:
    """Whether a dive's predictions agree well enough to auto-accept any of
    them, plus the fit metrics that decided it. Recorded even when eligible —
    the per-dive numbers are the monitoring signal."""

    eligible: bool
    reason: DiveIneligibleReason | None
    n_points: int
    inlier_count: int
    inlier_fraction: float
    line_confidence: float


@dataclass(frozen=True)
class FrameDecision:
    """One frame's verdict. `perpendicular_px` / `along_line_z` are recorded
    whenever a line existed, including for frames that pass, so a later
    audit can re-examine the margin rather than only the outcome."""

    image_id: int
    auto_accept: bool
    reason: FrameVerdict
    perpendicular_px: float | None = None
    along_line_z: float | None = None


def _along_line_z(projections: np.ndarray, inlier_mask: np.ndarray) -> np.ndarray:
    """Robust z-score of each point's position ALONG the line.

    Scale comes from the inliers' MAD so a slid point cannot inflate the
    spread it is being judged against.

    Known limitation: on a dive whose dots genuinely spread far along the line
    (a wide depth range), the MAD is large and this test goes toothless — the
    maximum z of a uniform distribution is ~1.35, well under any useful
    threshold. It bites when the dots cluster, which is the common case
    (prod depths average 1.75 m with most frames similar). A nearest-neighbour
    gap along the line would be scale-free and is the obvious upgrade if this
    proves too blunt; it is not implemented because the one real example
    available to calibrate against scored 5.2 on this measure.
    """
    ref = projections[inlier_mask] if inlier_mask.any() else projections
    median = float(np.median(ref))
    mad = float(np.median(np.abs(ref - median)))
    scale = max(MAD_TO_SIGMA * mad, LABEL_NOISE_MAD_FLOOR_PX)
    return np.abs(projections - median) / scale


def _is_audit_sample(dive_id: int, image_id: int, rate: float) -> bool:
    """Deterministic per-(dive, image) sampling.

    Keyed on the pair rather than drawn from an RNG so a re-run produces the
    same decisions — the cohort revisits dives, and a sample that re-rolled
    would change a dive's verdicts every pass. Including `dive_id` stops the
    same frame indices being audited on every dive, which would make the
    audit a fixed blind spot rather than a sample.
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.blake2b(
        f"{dive_id}:{image_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") / float(1 << 64) < rate


def evaluate_dive(
    dive_id: int,
    image_ids: list[int],
    points: np.ndarray,
    *,
    config: AutoAcceptConfig = DEFAULT_CONFIG,
) -> tuple[DiveGate, list[FrameDecision]]:
    """Decide the dive, then each frame.

    `points` is an (N, 2) array of predicted dot positions in rectified-image
    pixels, row-aligned with `image_ids`. A row of NaN means the detector
    abstained on that frame.

    Returns the dive-level gate and one decision per frame, in input order.
    """
    if len(image_ids) != points.shape[0]:
        raise ValueError("image_ids and points must be the same length")

    xy = np.asarray(points, dtype=float)
    has_point = np.isfinite(xy).all(axis=1)
    fittable = xy[has_point]

    def _ineligible(reason: DiveIneligibleReason, metrics: dict):
        gate = DiveGate(eligible=False, reason=reason, **metrics)
        return gate, [
            FrameDecision(
                image_id=image_id,
                auto_accept=False,
                reason=(
                    FrameVerdict.NO_PREDICTION
                    if not present
                    else FrameVerdict.DIVE_INELIGIBLE
                ),
            )
            for image_id, present in zip(image_ids, has_point)
        ]

    empty = {
        "n_points": int(has_point.sum()),
        "inlier_count": 0,
        "inlier_fraction": 0.0,
        "line_confidence": 0.0,
    }

    if int(has_point.sum()) < config.min_predictions:
        return _ineligible(DiveIneligibleReason.TOO_FEW_PREDICTIONS, empty)

    fit = fit_dive_line(fittable)
    if fit is None:
        return _ineligible(DiveIneligibleReason.NO_LINE, empty)

    metrics = {
        "n_points": fit.n_points,
        "inlier_count": fit.inlier_count,
        "inlier_fraction": fit.inlier_fraction,
        "line_confidence": fit.line_confidence,
    }

    # Fraction before confidence, and that order is load-bearing:
    # `line_confidence` is computed over the RANSAC *inliers*, which are by
    # construction a band no wider than the inlier tolerance. On a diffuse
    # dive RANSAC therefore hands the eigenratio an elongated subset and the
    # confidence test can read healthy while most of the dive disagrees. The
    # fraction is what actually notices that.
    if fit.inlier_fraction < config.min_inlier_fraction:
        return _ineligible(DiveIneligibleReason.WEAK_CONSENSUS, metrics)
    if fit.line_confidence < config.min_line_confidence:
        return _ineligible(DiveIneligibleReason.UNCONFIDENT_LINE, metrics)

    perp_present = fit.perpendicular_distance(fittable[:, 0], fittable[:, 1])
    tangent = np.array([-fit.b, fit.a])
    near_line = perp_present < config.max_perpendicular_px
    z_present = _along_line_z(fittable @ tangent, near_line)

    perp_all = np.full(xy.shape[0], np.nan)
    z_all = np.full(xy.shape[0], np.nan)
    perp_all[has_point] = perp_present
    z_all[has_point] = z_present

    decisions = _decide_frames(dive_id, image_ids, perp_all, z_all, config)
    return DiveGate(eligible=True, reason=None, **metrics), decisions


def _decide_frames(
    dive_id: int,
    image_ids: list[int],
    perp_all: np.ndarray,
    z_all: np.ndarray,
    config: AutoAcceptConfig,
) -> list[FrameDecision]:
    """Per-frame verdicts on a dive that already passed the gate.

    Order matters: the geometric tests come before the audit draw, so an
    audited frame is one that *would* have been auto-accepted. Reporting a
    frame as AUDIT_SAMPLE when it was really off-line would corrupt the very
    measurement the sample exists to provide.
    """
    decisions: list[FrameDecision] = []
    for i, image_id in enumerate(image_ids):
        perp, z_score = float(perp_all[i]), float(z_all[i])
        if not np.isfinite(perp):
            decisions.append(FrameDecision(image_id, False, FrameVerdict.NO_PREDICTION))
            continue
        if perp > config.max_perpendicular_px:
            verdict = FrameVerdict.OFF_LINE
        elif z_score > config.max_along_line_z:
            verdict = FrameVerdict.ALONG_LINE_OUTLIER
        elif _is_audit_sample(dive_id, image_id, config.audit_sample_rate):
            verdict = FrameVerdict.AUDIT_SAMPLE
        else:
            verdict = FrameVerdict.AUTO_ACCEPTED
        decisions.append(
            FrameDecision(
                image_id=image_id,
                # `enabled` gates only this flag, never the verdict. A
                # disabled gate still says what it would have done, which is
                # the entire value of a dark run; the row then reads
                # `auto_accept=False` beside `gate_verdict='auto_accepted'`,
                # which is not a contradiction but the switch overruling the
                # fit. Everything downstream keys on the flag.
                auto_accept=(config.enabled and verdict is FrameVerdict.AUTO_ACCEPTED),
                reason=verdict,
                perpendicular_px=perp,
                along_line_z=z_score,
            )
        )
    return decisions
