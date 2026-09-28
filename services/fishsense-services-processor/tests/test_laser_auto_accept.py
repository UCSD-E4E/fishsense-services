"""Unit tests for the laser auto-accept gate.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_laser_auto_accept.py: the policy ports into the
processor unchanged. In v2 `dive_id` and `image_id` are the dive's and the
capture's `number` (v1's ids for migrated rows), so the audit sample picks the
frames v1 picked.

The gate decides, per dive, whether the detector's predictions agree well
enough with each other to skip human review, and then per frame whether an
individual prediction may be auto-accepted.

Two axes, deliberately. Perpendicular distance alone is blind to a dot slid
ALONG the laser's epipolar line — the direction that moves depth most, and the
one real prod example (dive 491 image 132158: 1.5 px off-line, 190 px along it)
is invisible to it. Every test below that mentions "along" exists to keep that
case out of the auto-accept set.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from fishsense_services_processor.laser_validation.auto_accept import (
    DEFAULT_CONFIG,
    AutoAcceptConfig,
    DiveIneligibleReason,
    FrameVerdict,
    evaluate_dive,
)


def _collinear(
    n: int,
    *,
    slope: float = 0.4,
    intercept: float = 500.0,
    spacing: float = 30.0,
    jitter: float = 0.0,
    seed: int = 0,
) -> np.ndarray:
    """`n` points along a line, optionally with sub-pixel perpendicular noise."""
    rng = np.random.default_rng(seed)
    x = 800.0 + spacing * np.arange(n, dtype=float)
    y = intercept + slope * x
    if jitter:
        nrm = np.array([-slope, 1.0]) / np.hypot(slope, 1.0)
        y = y + rng.normal(0.0, jitter, size=n) * nrm[1]
        x = x + rng.normal(0.0, jitter, size=n) * nrm[0]
    return np.column_stack([x, y])


def _ids(n: int, start: int = 1000) -> list[int]:
    return list(range(start, start + n))


# --- dive-level participation gate ------------------------------------------


def test_dive_below_frame_floor_is_ineligible():
    """A handful of collinear points is not consensus. Five points pass the
    line fit's own MIN_POINTS_FOR_LINE, which is why the gate needs its own,
    higher floor: at n=5 three bad points are a majority and RANSAC only needs
    two to hypothesise."""
    xy = _collinear(8)
    gate, verdicts = evaluate_dive(1, _ids(8), xy)
    assert not gate.eligible
    assert gate.reason is DiveIneligibleReason.TOO_FEW_PREDICTIONS
    assert all(not v.auto_accept for v in verdicts)


def test_dive_below_inlier_fraction_is_ineligible():
    """The v1-detector shape: plenty of frames, but they do not agree.
    Measured in prod, fitting on predictions: v1 dives scored 0.369-0.679
    inlier fraction, v2 dives 0.842-1.000. The bar sits in that gap."""
    good = _collinear(30)
    scatter = np.column_stack([np.linspace(500, 3500, 30), np.linspace(2500, 400, 30)])
    gate, verdicts = evaluate_dive(1, _ids(60), np.vstack([good, scatter]))
    assert not gate.eligible
    assert gate.reason is DiveIneligibleReason.WEAK_CONSENSUS
    assert gate.inlier_fraction < DEFAULT_CONFIG.min_inlier_fraction
    assert all(not v.auto_accept for v in verdicts)


def test_dive_with_unconfident_line_is_ineligible():
    """A blob of dots has no direction; the eigenratio test must catch it
    before any per-frame distance is consulted."""
    rng = np.random.default_rng(3)
    # Tight on purpose: nearly every point is within the RANSAC tolerance of
    # any line through the blob, so `inlier_fraction` stays high and this test
    # isolates the confidence axis. A loose blob is caught by WEAK_CONSENSUS
    # first — see the ordering comment in `evaluate_dive`.
    blob = rng.normal(2000.0, 1.5, size=(40, 2))
    gate, verdicts = evaluate_dive(1, _ids(40), blob)
    assert not gate.eligible
    assert gate.reason is DiveIneligibleReason.UNCONFIDENT_LINE
    assert all(not v.auto_accept for v in verdicts)


def test_eligible_dive_reports_its_fit_metrics():
    xy = _collinear(40, jitter=0.6, seed=5)
    gate, _ = evaluate_dive(1, _ids(40), xy)
    assert gate.eligible
    assert gate.reason is None
    assert gate.n_points == 40
    assert gate.inlier_fraction >= DEFAULT_CONFIG.min_inlier_fraction
    assert gate.line_confidence >= DEFAULT_CONFIG.min_line_confidence


# --- per-frame decisions -----------------------------------------------------


def test_on_line_in_distribution_points_are_auto_accepted():
    xy = _collinear(40, jitter=0.5, seed=7)
    gate, verdicts = evaluate_dive(1, _ids(40), xy, config=_no_audit())
    assert gate.eligible
    assert all(v.auto_accept for v in verdicts)
    assert all(v.reason is FrameVerdict.AUTO_ACCEPTED for v in verdicts)


def test_off_line_point_is_routed_to_a_human():
    xy = _collinear(40, jitter=0.4, seed=9)
    normal = np.array([-0.4, 1.0]) / np.hypot(0.4, 1.0)
    xy[17] = xy[17] + normal * 60.0
    _, verdicts = evaluate_dive(1, _ids(40), xy, config=_no_audit())
    off = verdicts[17]
    assert not off.auto_accept
    assert off.reason is FrameVerdict.OFF_LINE
    assert off.perpendicular_px > DEFAULT_CONFIG.max_perpendicular_px
    assert sum(v.auto_accept for v in verdicts) == 39


def test_along_line_outlier_is_routed_even_though_it_sits_on_the_line():
    """The dive-491 case. The point is perfectly collinear, so a
    perpendicular-only gate accepts it; it is 190 px from where the labeler
    put it, along the epipolar direction, which is what corrupts depth."""
    xy = _collinear(40, jitter=0.3, spacing=4.0, seed=11)
    tangent = np.array([1.0, 0.4]) / np.hypot(1.0, 0.4)
    xy[22] = xy[22] + tangent * 900.0
    _, verdicts = evaluate_dive(1, _ids(40), xy, config=_no_audit())
    slid = verdicts[22]
    assert slid.perpendicular_px <= DEFAULT_CONFIG.max_perpendicular_px
    assert not slid.auto_accept
    assert slid.reason is FrameVerdict.ALONG_LINE_OUTLIER
    assert slid.along_line_z > DEFAULT_CONFIG.max_along_line_z


def test_frames_without_a_prediction_always_route_to_a_human():
    """An abstention is not a disagreement — but it is also not a label, so
    the frame still needs a person."""
    xy = _collinear(40, jitter=0.4, seed=13)
    xy[5] = [np.nan, np.nan]
    _, verdicts = evaluate_dive(1, _ids(40), xy, config=_no_audit())
    assert not verdicts[5].auto_accept
    assert verdicts[5].reason is FrameVerdict.NO_PREDICTION


# --- audit sample ------------------------------------------------------------


def test_audit_sample_diverts_a_share_of_otherwise_accepted_frames():
    xy = _collinear(200, jitter=0.4, seed=17)
    _, verdicts = evaluate_dive(1, _ids(200), xy)
    audited = [v for v in verdicts if v.reason is FrameVerdict.AUDIT_SAMPLE]
    assert all(not v.auto_accept for v in audited)
    assert 0.04 <= len(audited) / 200 <= 0.18


def test_audit_selection_is_stable_across_reruns_and_ordering():
    """Keyed on (dive_id, image_id), not on a draw order — a re-run must not
    re-roll the sample, or a dive's decisions change every time the cohort
    revisits it."""
    xy = _collinear(120, jitter=0.4, seed=19)
    ids = _ids(120)
    _, first = evaluate_dive(7, ids, xy)
    _, again = evaluate_dive(7, ids, xy)
    assert [v.reason for v in first] == [v.reason for v in again]

    order = np.arange(120)[::-1]
    _, reversed_run = evaluate_dive(7, [ids[i] for i in order], xy[order])
    by_id = {v.image_id: v.reason for v in reversed_run}
    assert all(by_id[v.image_id] == v.reason for v in first)


def test_audit_sample_differs_between_dives():
    """Same image indices, different dive: the sampled set must not be the
    same frames every time, or the audit is a fixed blind spot."""
    xy = _collinear(200, jitter=0.4, seed=23)
    ids = _ids(200)
    _, a = evaluate_dive(1, ids, xy)
    _, b = evaluate_dive(2, ids, xy)
    sel_a = {v.image_id for v in a if v.reason is FrameVerdict.AUDIT_SAMPLE}
    sel_b = {v.image_id for v in b if v.reason is FrameVerdict.AUDIT_SAMPLE}
    assert sel_a != sel_b


def test_audit_rate_of_zero_accepts_everything_eligible():
    xy = _collinear(60, jitter=0.4, seed=29)
    _, verdicts = evaluate_dive(1, _ids(60), xy, config=_no_audit())
    assert all(v.auto_accept for v in verdicts)


def _no_audit() -> AutoAcceptConfig:
    """Config with the audit sample disabled, so per-frame geometry tests
    aren't perturbed by a frame being randomly diverted."""
    return replace(DEFAULT_CONFIG, audit_sample_rate=0.0)


# --- config ------------------------------------------------------------------


def test_defaults_match_the_measured_calibration():
    """These numbers were measured, not chosen: the 0.75 fraction sits in the
    0.679-0.842 gap between v1 and v2 dives fitted on predictions, and the
    10 px band is where the prod separation held (19/22 moves caught, 3/465
    accepted wrongly flagged)."""
    assert DEFAULT_CONFIG.min_inlier_fraction == 0.75
    assert DEFAULT_CONFIG.min_predictions == 20
    assert DEFAULT_CONFIG.max_perpendicular_px == 10.0
    assert DEFAULT_CONFIG.max_along_line_z == 4.0
    assert DEFAULT_CONFIG.audit_sample_rate == 0.10


def test_config_rejects_a_floor_below_the_line_fits_own_minimum():
    with pytest.raises(ValueError):
        AutoAcceptConfig(min_predictions=3)


# --- the kill switch ---------------------------------------------------------


def test_enabled_defaults_to_true():
    assert DEFAULT_CONFIG.enabled is True


def test_a_disabled_gate_accepts_nothing():
    """`enabled=False` is the kill switch AND the dark run. Nothing may skip
    review while it is off."""
    xy = _collinear(40, jitter=0.4, seed=31)
    config = replace(DEFAULT_CONFIG, enabled=False, audit_sample_rate=0.0)
    gate, verdicts = evaluate_dive(1, _ids(40), xy, config=config)
    assert gate.eligible
    assert not any(v.auto_accept for v in verdicts)


def test_a_disabled_gate_still_records_what_it_would_have_done():
    """This is what makes it a dark run rather than an off switch. The verdict
    and both margins are still computed and recorded, so a week of running
    disabled answers "what would this have done to our corpus" exactly, on
    current data, without a single frame skipping a human.

    The consequence is a row reading `auto_accept=False` beside
    `gate_verdict='auto_accepted'`. That pair is not a contradiction — it says
    the gate would have cleared this frame and the switch overruled it — and
    populate keys on `auto_accept` alone, so the disabled state is what
    governs behaviour.
    """
    xy = _collinear(40, jitter=0.4, seed=33)
    config = replace(DEFAULT_CONFIG, enabled=False, audit_sample_rate=0.0)
    _, verdicts = evaluate_dive(1, _ids(40), xy, config=config)
    assert all(v.reason is FrameVerdict.AUTO_ACCEPTED for v in verdicts)
    assert all(v.perpendicular_px is not None for v in verdicts)
    assert all(v.along_line_z is not None for v in verdicts)


def test_a_disabled_gate_still_distinguishes_its_rejections():
    """Turning the gate off must not flatten the verdict histogram — that
    histogram is the monitoring signal, and it is the only thing a dark run
    produces."""
    xy = _collinear(40, jitter=0.4, seed=35)
    normal = np.array([-0.4, 1.0]) / np.hypot(0.4, 1.0)
    xy[9] = xy[9] + normal * 80.0
    config = replace(DEFAULT_CONFIG, enabled=False, audit_sample_rate=0.0)
    _, verdicts = evaluate_dive(1, _ids(40), xy, config=config)
    assert verdicts[9].reason is FrameVerdict.OFF_LINE
    assert sum(v.reason is FrameVerdict.AUTO_ACCEPTED for v in verdicts) == 39
