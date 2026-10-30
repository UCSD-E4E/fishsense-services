"""Label-free size-constancy calibration on the paper's sessions, as the
calibration activity runs it.

The method's code is fishsense-core's `fishsense_core.size_constancy` (core
#97), which absorbed this repo's port (`automatic_results/size_constancy.py`,
deleted) of cscw-fishsense2027@96a8da07 calibration_analysis/
e1j_size_constancy/register.py and slate_unknown.py. Core's own suite carries
the synthetic, refusal and registration tests that used to live here; what
stays is the golden check of the fit the activity
(`automatic_calibration.activities`) actually calls, so a core upgrade that
moves a paper session fails here.

**Golden**: `fixtures/size_constancy_golden.json` holds, for the paper's ten
calibration sessions (Table 3), the pair equations cscw's own `pair_scale`
produced from its SIFT features, the dots, the dive line, the camera matrix
and the stored calibration's axis; and cscw's output (`labelfree_tv.csv`).

**Session 114 moved** with the port to core (2026-10-23). Frame 7's pairs
include one at log scale -37.18, which drags its first solve so far that every
pair through it fails the robust pass. The services copy still counted it
connected and solved it the size the gauge made up; core sizes only the
largest connected component of the pairs the robust pass kept, so frame 7 has
no size. The solve: 29 -> 28 frames, bootstrap SE (cscw's shared generator)
4.41 -> 0.73 px. The fit: the services copy's trim had dropped frame 7 too,
so it still uses 28 frames, but sized from a different solve: vanishing
position -2273.594 -> -2273.784 px, error against the stored calibration
-0.0446 -> -0.0485 deg (cscw: -0.129). The other nine sessions are unchanged
(to 1e-11 px).

**Session 114 moved back** with fishsense-core 4.3.2 (core #113, 2026-10-25):
the solve drops a pair beyond 10x (`MAX_PAIR_LOG_SCALE`) before its first
pass, and counts it as implausible. The -37.18 pair no longer drags frame 7,
so all 29 frames are sized and fitted again: vanishing position -2273.784 ->
-2273.558 px, error against the stored (known-size) calibration -0.0485 ->
-0.0439 deg (cscw: -0.129), bootstrap SE 0.73 -> 0.69 px (cscw: 4.41). The
other nine are unchanged.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pytest

# The module the calibration activity fits with: the test follows the call site.
from fishsense_services_processor.automatic_calibration.activities import sc

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "size_constancy_golden.json").read_text()
)["dives"]
#: cscw's nominal focal length for turning pixels into degrees (slate_unknown.F_PX).
F_PX = 2850.0
#: The one session whose solve departs from cscw's (see the module docstring).
_CSCW_DEPARTS = {"114"}


def _pairs(d):
    return [sc.PairScale(int(i), int(j), float(v), int(n)) for i, j, v, n in d["pairs"]]


def _stored_vanishing(d, u):
    """The stored (known-size) calibration's vanishing point, along the line."""
    k = np.array(d["camera_matrix"])
    ax = np.array(d["stored_axis"])
    v = np.array([k[0, 0] * ax[0] / ax[2] + k[0, 2], k[1, 1] * ax[1] / ax[2] + k[1, 2]])
    return float(v @ u)


def _fit(d):
    return sc.fit_label_free(
        dots=np.array(d["dots"]), pairs=_pairs(d), camera_matrix=d["camera_matrix"],
        line=d["line"], min_frames=1, min_size_ratio=1.0,
    )  # fmt: skip


@pytest.mark.parametrize("mission", sorted(set(GOLDEN) - _CSCW_DEPARTS, key=int))
def test_reproduces_cscws_label_free_fit(mission):
    d = GOLDEN[mission]
    want = d["expected"]
    dots = np.array(d["dots"])

    sizes = sc.solve_log_sizes(len(dots), _pairs(d))
    u = sc.line_direction(d["line"], dots[sizes.connected])
    t = dots[sizes.connected] @ u
    tv = sc.fit_vanishing_position(t, np.exp(sizes.log_sizes[sizes.connected]))

    assert int(sizes.connected.sum()) == want["frames"]
    assert u == pytest.approx([want["ux"], want["uy"]], abs=1e-12)
    assert sizes.residual_sd == pytest.approx(want["pair_resid_sd"], rel=1e-6)
    assert np.ptp(sizes.log_sizes[sizes.connected]) == pytest.approx(
        np.log(want["size_ratio"]), rel=1e-9
    )
    assert tv == pytest.approx(want["tv_fit"], abs=1e-4)
    assert tv - _stored_vanishing(d, u) == pytest.approx(want["err_px"], abs=1e-4)


def test_session_114s_frame_7_is_sized_without_its_collapsed_pair():
    """Re-pinned (2026-10-25, core 4.3.2): cscw's 29 frames at -2277.786 px;
    4.3.0 sized 28 (frame 7 out) at -2273.784 px; 4.3.2 drops the -37.18
    pair and sizes all 29, at -2273.558 px."""
    d = GOLDEN["114"]
    dots = np.array(d["dots"])
    sizes = sc.solve_log_sizes(len(dots), _pairs(d))
    assert sizes.connected.all()
    u = sc.line_direction(d["line"], dots[sizes.connected])
    t = dots[sizes.connected] @ u
    tv = sc.fit_vanishing_position(t, np.exp(sizes.log_sizes[sizes.connected]))
    assert tv == pytest.approx(-2273.5578981324384, abs=1e-4)


def test_bootstrap_reproduces_cscws_standard_errors():
    """cscw draws every session's bootstrap from one generator, in Table 3's
    order; so must this, to reproduce its SEs. Session 114 is last, so the
    draws before it are unchanged; re-pinned (2026-10-25, core 4.3.2), its
    own SE is 0.69 px with the collapsed pair out of the solve (cscw: 4.41;
    4.3.0, with frame 7 out: 0.73)."""
    rng = np.random.default_rng(0)
    for mission in sorted(GOLDEN, key=int):
        d = GOLDEN[mission]
        dots = np.array(d["dots"])
        sizes = sc.solve_log_sizes(len(dots), _pairs(d))
        xy = dots[sizes.connected]
        t = xy @ sc.line_direction(d["line"], xy)
        se = sc.bootstrap_se(t, np.exp(sizes.log_sizes[sizes.connected]), rng=rng)
        want = (
            0.6859628145635921 if mission in _CSCW_DEPARTS else d["expected"]["se_px"]
        )
        assert se == pytest.approx(want, rel=1e-6), mission


def test_table_3_all_ten_within_0_05_deg():
    """cscw: 9 of 10 within 0.05 deg, all within 0.15. Session 114 (0.129 deg
    in cscw) is 0.0439 deg."""
    errors = []
    for d in GOLDEN.values():
        fit = _fit(d)
        u = np.array(fit.line_direction)
        errors.append(np.degrees((fit.vanishing_px - _stored_vanishing(d, u)) / F_PX))
    errors = np.abs(errors)
    assert (errors <= 0.05).all()
    assert np.median(errors) == pytest.approx(0.02, abs=0.01)


@pytest.mark.parametrize("mission", sorted(set(GOLDEN) - _CSCW_DEPARTS, key=int))
def test_nine_paper_sessions_are_fitted_as_cscw_fitted_them(mission):
    """cscw's frames (human dots on labeled slate frames) are consistent, so
    the trim drops none of theirs."""
    d = GOLDEN[mission]
    fit = _fit(d)
    assert fit.frames_used == d["expected"]["frames"]
    assert fit.vanishing_px == pytest.approx(d["expected"]["tv_fit"], abs=1e-4)


def test_session_114_is_fitted_on_all_29_frames():
    """Re-pinned (2026-10-25, core 4.3.2): 29 frames (4.3.0: 28, frame 7
    out), vanishing position -2273.784 -> -2273.558 px, error against the
    stored calibration -0.0485 -> -0.0439 deg, SE 0.812 -> 0.781 px; the
    collapsed pair is counted as implausible, not matched."""
    d = GOLDEN["114"]
    fit = _fit(d)
    u = np.array(fit.line_direction)
    err_deg = np.degrees((fit.vanishing_px - _stored_vanishing(d, u)) / F_PX)
    assert fit.frames_used == d["expected"]["frames"] == 29
    assert 7 in fit.used
    assert fit.implausible_pairs == 1
    assert fit.vanishing_px == pytest.approx(-2273.5578981324384, abs=1e-4)
    assert err_deg == pytest.approx(-0.04390275434929713, abs=1e-6)
    assert fit.se_px == pytest.approx(0.781313857611461, rel=1e-6)


# -- without a dive line (review of #354) ------------------------------------
#
# The activity passes `line=None` when the mission has no confident line, and
# core then takes the direction through the dots it keeps, re-fitting it after
# each trim. Pinned on the ten sessions with the line withheld, against the
# same control as above: the stored (known-size) calibration.

#: (frames used, vanishing position px) without the line, core 4.3.2.
_NO_LINE = {
    "62": (20, -2049.669730655789),
    "63": (12, -2106.0189165834586),
    "65": (27, -2084.9796868369435),
    "71": (32, -2136.90081293668),
    "77": (22, -2069.6919848884604),
    "80": (31, -2076.036068668379),
    "83": (26, -2047.5353760465407),
    "87": (30, -2072.8979665605602),
    "94": (21, -1993.2297521417825),
    "114": (29, -2280.7259250211364),
}


@pytest.mark.parametrize("mission", sorted(GOLDEN, key=int))
def test_without_a_dive_line_each_session_fits_as_well(mission):
    """Same frames as with the line, and within the paper's 0.05 deg of the
    stored calibration (the largest is 0.044). The direction moves by up to
    2.4 deg (session 94) and the error does not."""
    d = GOLDEN[mission]
    fit = sc.fit_label_free(
        dots=np.array(d["dots"]), pairs=_pairs(d), camera_matrix=d["camera_matrix"],
        line=None,
    )  # fmt: skip
    frames, tv = _NO_LINE[mission]
    u = np.array(fit.line_direction)
    err_deg = np.degrees((fit.vanishing_px - _stored_vanishing(d, u)) / F_PX)
    assert fit.outcome == "accepted"
    assert fit.frames_used == frames == d["expected"]["frames"]
    assert fit.vanishing_px == pytest.approx(tv, abs=1e-3)
    assert abs(err_deg) <= 0.05


# -- the refusals the activity stores (review of #354) ------------------------
#
# `refusal_reason` reaches `automatic_laser_calibrations` verbatim, so a core
# release that renames one must fail here, not in production's rows.


def _beyond_the_bracket(n=12, tv=-6000.0):
    """Sizes consistent with a vanishing position past the bracket's far
    edge, every pair matched: the fit's optimum is the bracket's edge."""
    t = 400.0 * np.arange(n) + 200
    s = t - tv
    pairs = [
        sc.PairScale(i, j, float(np.log(s[j] / s[i])), 60)
        for i, j in itertools.combinations(range(n), 2)
    ]
    return np.column_stack([t, np.full(n, 900.0)]), pairs


def test_each_refusal_reason_the_fit_can_store():
    d = GOLDEN["62"]
    dots, k = np.array(d["dots"]), d["camera_matrix"]
    first3 = [p for p in _pairs(d) if p.i < 3 and p.j < 3]
    xy, pairs = _beyond_the_bracket()
    reasons = [
        sc.fit_label_free(dots=dots, pairs=[], camera_matrix=k, line=d["line"]),
        sc.fit_label_free(dots=dots[:3], pairs=first3, camera_matrix=k, line=d["line"]),
        sc.fit_label_free(dots=dots, pairs=_pairs(d), camera_matrix=k,
                          line=d["line"], min_size_ratio=1e6),
        sc.fit_label_free(dots=xy, pairs=pairs, camera_matrix=k,
                          line=(0.0, 1.0, -900.0)),
    ]  # fmt: skip
    assert [r.refusal_reason for r in reasons] == [
        "too_few_matched_pairs",
        "too_few_frames",
        "too_little_range_spread",
        "unconstrained",
    ]
    assert all(r.outcome == "refused" and r.laser_position is None for r in reasons)
