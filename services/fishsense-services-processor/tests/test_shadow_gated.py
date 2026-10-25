"""The shadow calibration's gated variant: the committed gate and refusals.

docs/shadow-size-constancy.md §13 commits the values (author, 2026-10-21,
before any backfill): the detector-dot consistency gate (confidence, the
detector line, an ambiguous second line, range consistency) and the refusals
(frames, size spread, production's own 0.6 m lever arm, bootstrap SE, SAM
scores, an unconstrained optimum, an ambiguous line). The `port` variant is
the script exactly (test_shadow_fit.py); this is what the switching criteria
judge.

On the pool sessions the counts must be the ones reported to the author
(§6.1, analysis/shadow_calibration/pool_triggers.py): no label-dot session
refused; the detector gate drops 4 frames on confidence (65: 3, 83: 1) and
23 off the line (77: 22, 114: 1), and refuses 77 for an ambiguous line; the
trim fires on none. (§6.1 first said 5 on confidence, putting 114's frame
there; the analysis output and this test both say off the line.)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fishsense_services_processor.shadow_calibration.fit import VARIANT_INDEX
from fishsense_services_processor.shadow_calibration.gated import (
    COMMITTED,
    Frame,
    detector_gate,
    gated_fit,
)

POOL = json.loads((Path(__file__).parent / "fixtures/shadow_pool.json").read_text())
SESSIONS = {s["session"]: s for s in POOL["sessions"]}
#: Sizes are sqrt-area at the port's 1600-px render; dots are full-res.
SCALE = 4014 / 1600


def _label_frames(session):
    return [Frame(capture_id=f["image_id"], x=f["dot_x"], y=f["dot_y"], size=f["size"],
                  sam_score=f["sam_score"]) for f in session["frames"]]  # fmt: skip


def _fit(session, frames, *, u=None, off=None, source="labels", ambiguous=False,
         thresholds=COMMITTED):  # fmt: skip
    line = session["line"]
    return gated_fit(
        frames,
        u=np.array(u if u is not None else line["u"]),
        off=off if off is not None else line["offset"],
        K=np.array(session["camera_matrix"]),
        reference_axis=np.array(session["stored_laser_axis"]),
        reference_position=np.array(session["stored_laser_position"]),
        seed=(session["session"], VARIANT_INDEX["sam3_all" if source == "labels"
                                                else "sam3_predline"]),
        size_to_full_px=SCALE,
        ambiguous_line=ambiguous,
        thresholds=thresholds,
    )  # fmt: skip


def test_the_committed_values_are_section_13s():
    c = COMMITTED
    assert (c.min_confidence, c.off_line_px, c.second_line_min, c.second_line_share,
            c.residual_max) == (0.5, 10.0, 8, 0.25, 0.10)  # fmt: skip
    assert (c.min_frames, c.min_size_spread, c.max_se_deg, c.min_median_sam_score,
            c.max_no_mask_share) == (8, 1.5, 0.05, 0.5, 0.25)  # fmt: skip
    assert c.refusal_order == (
        "too_few_frames", "too_little_size_spread", "lever_arm_below_0.6m",
        "se_too_high", "sam_scores_low", "unconstrained", "ambiguous_line",
    )  # fmt: skip


def test_the_lever_arm_is_productions_own_gate():
    from fishsense_core.calibration_checks import (
        MIN_OBSERVATION_LEVER_M,
    )

    assert MIN_OBSERVATION_LEVER_M == 0.60


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_every_pool_session_is_accepted_on_its_label_dots(session):
    s = SESSIONS[session]
    r = _fit(s, _label_frames(s))

    assert r.outcome == "accepted", r.refusals
    assert r.refusals == () and r.trimmed == ()
    # The fit is the port's on these frames (no frame was trimmed).
    assert r.fit.err_deg_f2850 == pytest.approx(
        s["published"]["sam3_all"]["err_deg"], abs=5.000001e-6
    )
    assert r.lever_m >= 1.0
    assert 0.5 < r.size_cv_pct < 2.0


def _detector(session):
    s = SESSIONS[session]
    slate = {f["image_id"]: f for f in s["frames"]}
    dots = {d["image_id"]: d for d in s["detector_dots"]}
    gate = detector_gate(
        slate_dots=[(i, dots[i]["x"], dots[i]["y"], dots[i]["confidence"])
                    for i in slate if i in dots],
        mission_dots=np.array([[d["x"], d["y"], d["confidence"]] for d in s["detector_dots"]]),
    )  # fmt: skip
    frames = [Frame(capture_id=i, x=dots[i]["x"], y=dots[i]["y"], size=slate[i]["size"],
                    sam_score=slate[i]["sam_score"], frame_source="label")
              for i in gate.kept]  # fmt: skip
    return s, gate, frames


def test_the_detector_gate_drops_what_was_reported_on_the_pool():
    counts = {}
    for session in sorted(SESSIONS):
        _, gate, _ = _detector(session)
        for reason in gate.rejected.values():
            counts.setdefault(reason, {}).setdefault(session, 0)
            counts[reason][session] += 1
        assert gate.ambiguous_line == (session == 77), session
    assert counts == {"confidence_low": {65: 3, 83: 1},
                      "off_line": {77: 22, 114: 1}}  # fmt: skip


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_on_detector_dots_only_77_is_refused(session):
    s, gate, frames = _detector(session)
    r = _fit(s, frames, u=gate.u, off=gate.offset, source="detector",
             ambiguous=gate.ambiguous_line)  # fmt: skip
    if session == 77:
        assert r.outcome == "refused"
        assert set(r.refusals) == {"too_few_frames", "ambiguous_line"}
    else:
        assert r.outcome == "accepted", r.refusals
        assert r.trimmed == ()


def test_red_specks_on_the_line_at_the_wrong_range_are_trimmed():
    """Dive 279's failure, in a sound session: two dots moved along the line to
    where a nearer target would put them (as 78686/78688 were, 0.42-0.49 m by
    the beam against 0.91-0.98 m by size). No line gate can see them; the
    range-consistency trim must drop exactly those two."""
    s = SESSIONS[80]
    frames = _label_frames(s)
    u = np.array(s["line"]["u"])
    moved = []
    for k in (3, 11):
        f = frames[k]
        p = np.array([f.x, f.y]) + 350.0 * u  # 350 px further along the line
        frames[k] = Frame(capture_id=f.capture_id, x=p[0], y=p[1], size=f.size,
                          sam_score=f.sam_score)  # fmt: skip
        moved.append(f.capture_id)

    r = _fit(s, frames)

    assert sorted(r.trimmed) == sorted(moved)
    assert r.outcome == "accepted"


def test_dive_279s_specks_leave_it_refused():
    """On 279's own rows the specks (78686, 78688) are trimmed, and what is
    left is too few frames: 279 stays unmeasurable, as IMWUT found."""
    rows = [r for r in POOL["dive279"] if r["size"] is not None]
    frames = [Frame(capture_id=r["image_id"], x=r["dot_x"], y=r["dot_y"], size=r["size"],
                    sam_score=0.9) for r in rows]  # fmt: skip
    xy = np.array([[f.x, f.y] for f in frames])
    gate = detector_gate(
        slate_dots=[(f.capture_id, f.x, f.y, 1.0) for f in frames],
        mission_dots=np.column_stack([xy, np.ones(len(xy))]),
    )
    kept = [f for f in frames if f.capture_id in gate.kept]
    s = SESSIONS[62]  # any camera: the refusal does not depend on it
    r = _fit(s, kept, u=gate.u, off=gate.offset, source="detector")

    assert r.outcome == "refused"
    assert {78686, 78688} <= set(r.trimmed)
    assert "too_few_frames" in r.refusals


# -- each refusal, alone ----------------------------------------------------------


def _session_frames(session=80):
    s = SESSIONS[session]
    return s, [f for f in _label_frames(s) if f.size is not None]


def test_too_few_frames():
    s, frames = _session_frames()
    r = _fit(s, frames[:7])
    assert r.refusals[0] == "too_few_frames"


def test_too_little_size_spread():
    s, frames = _session_frames()
    frames.sort(key=lambda f: f.size)
    mid = frames[len(frames) // 2 - 6 : len(frames) // 2 + 6]  # a narrow band of sizes
    r = _fit(s, mid)
    assert mid[-1].size / mid[0].size < 1.5
    assert "too_little_size_spread" in r.refusals


def test_a_lever_arm_under_0_6_m_is_refused():
    s, frames = _session_frames()
    ranges = _fit(s, frames).ranges_m
    # The frames within 0.4 m of one range: enough of them, too little lever.
    by_range = sorted(frames, key=lambda f: ranges[f.capture_id])
    best = max(
        ([f for f in by_range if 0 <= ranges[f.capture_id] - ranges[g.capture_id] <= 0.4]
         for g in by_range),
        key=len,
    )  # fmt: skip
    assert len(best) >= 8
    r = _fit(s, best, thresholds=COMMITTED.but(min_size_spread=1.0, max_se_deg=10.0))
    assert r.lever_m < 0.6
    assert "lever_arm_below_0.6m" in r.refusals


def test_a_high_bootstrap_se_is_refused():
    s, frames = _session_frames()
    rng = np.random.default_rng(1)
    noisy = [Frame(capture_id=f.capture_id, x=f.x, y=f.y,
                   size=f.size * float(np.exp(rng.normal(0, 0.25))), sam_score=f.sam_score)
             for f in frames]  # fmt: skip
    r = _fit(s, noisy, thresholds=COMMITTED.but(residual_max=10.0))
    assert r.fit.se_deg > 0.05
    assert "se_too_high" in r.refusals


@pytest.mark.parametrize("case", ["median", "no_mask"])
def test_low_sam_scores_are_refused(case):
    s, frames = _session_frames()
    if case == "median":
        frames = [Frame(capture_id=f.capture_id, x=f.x, y=f.y, size=f.size, sam_score=0.4)
                  for f in frames]  # fmt: skip
    else:
        n = len(frames)
        frames = frames + [Frame(capture_id=-k, x=2000.0, y=1400.0, size=None, sam_score=None)
                           for k in range(1, n // 3 + 2)]  # fmt: skip
    r = _fit(s, frames)
    assert "sam_scores_low" in r.refusals


def test_an_optimum_at_the_bracket_edge_is_refused():
    s, frames = _session_frames()
    u = np.array(s["line"]["u"])
    # Sizes that grow along the line with no vanishing point in reach.
    flat = [Frame(capture_id=f.capture_id, x=f.x, y=f.y,
                  size=float(np.exp(-(np.array([f.x, f.y]) @ u) / 100.0)),
                  sam_score=f.sam_score) for f in frames]  # fmt: skip
    r = _fit(s, flat, thresholds=COMMITTED.but(residual_max=10.0))
    assert "unconstrained" in r.refusals


def test_the_first_reason_in_the_committed_order_is_the_refusal_reason():
    s, frames = _session_frames()
    r = _fit(s, frames[:7], ambiguous=True)
    assert r.refusal_reason == "too_few_frames"
    assert r.refusals[-1] == "ambiguous_line"


def test_the_production_offset_lever_is_recorded_and_decides_nothing():
    s, frames = _session_frames()
    r = _fit(s, frames)
    o = float(np.linalg.norm(s["stored_laser_position"][:2]))
    assert r.lever_m_production_offset is not None and o != 0.104
    assert abs(r.lever_m_production_offset - r.lever_m) < 0.2
