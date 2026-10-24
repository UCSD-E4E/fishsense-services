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
"""

from __future__ import annotations

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


def test_session_114s_frame_7_has_no_size():
    """Re-pinned (2026-10-23): cscw's 29 frames, its vanishing position
    -2277.786 px; now 28, -2273.784 px."""
    d = GOLDEN["114"]
    dots = np.array(d["dots"])
    sizes = sc.solve_log_sizes(len(dots), _pairs(d))
    assert np.flatnonzero(~sizes.connected).tolist() == [7]
    u = sc.line_direction(d["line"], dots[sizes.connected])
    t = dots[sizes.connected] @ u
    tv = sc.fit_vanishing_position(t, np.exp(sizes.log_sizes[sizes.connected]))
    assert tv == pytest.approx(-2273.7843259011893, abs=1e-4)


def test_bootstrap_reproduces_cscws_standard_errors():
    """cscw draws every session's bootstrap from one generator, in Table 3's
    order; so must this, to reproduce its SEs. Session 114 is last, so the
    draws before it are unchanged; re-pinned (2026-10-23), its own SE falls
    from cscw's 4.41 px to 0.73 px with frame 7 out of the solve."""
    rng = np.random.default_rng(0)
    for mission in sorted(GOLDEN, key=int):
        d = GOLDEN[mission]
        dots = np.array(d["dots"])
        sizes = sc.solve_log_sizes(len(dots), _pairs(d))
        xy = dots[sizes.connected]
        t = xy @ sc.line_direction(d["line"], xy)
        se = sc.bootstrap_se(t, np.exp(sizes.log_sizes[sizes.connected]), rng=rng)
        want = (
            0.7291465961104082 if mission in _CSCW_DEPARTS else d["expected"]["se_px"]
        )
        assert se == pytest.approx(want, rel=1e-6), mission


def test_table_3_all_ten_within_0_05_deg():
    """cscw: 9 of 10 within 0.05 deg, all within 0.15. Session 114 (0.129 deg
    in cscw) is 0.0485 deg."""
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
    """cscw's frames (human dots on labelled slate frames) are consistent, so
    the trim drops none of theirs."""
    d = GOLDEN[mission]
    fit = _fit(d)
    assert fit.frames_used == d["expected"]["frames"]
    assert fit.vanishing_px == pytest.approx(d["expected"]["tv_fit"], abs=1e-4)


def test_session_114_is_fitted_without_frame_7():
    """Re-pinned (2026-10-23): vanishing position -2273.594 -> -2273.784 px,
    error against the stored calibration -0.0446 -> -0.0485 deg, SE 0.815 ->
    0.812 px; 28 frames before and after, frame 7 left out."""
    d = GOLDEN["114"]
    fit = _fit(d)
    u = np.array(fit.line_direction)
    err_deg = np.degrees((fit.vanishing_px - _stored_vanishing(d, u)) / F_PX)
    assert fit.frames_used == d["expected"]["frames"] - 1 == 28
    assert 7 not in fit.used
    assert fit.vanishing_px == pytest.approx(-2273.784325901134, abs=1e-4)
    assert err_deg == pytest.approx(-0.048454808914494524, abs=1e-6)
    assert fit.se_px == pytest.approx(0.8122183526312848, rel=1e-6)
