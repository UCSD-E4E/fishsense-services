"""Label-free size-constancy laser calibration: the pure port, pinned.

Ported from cscw-fishsense2027@96a8da07
calibration_analysis/e1j_size_constancy/register.py (`pair_scale`, `score`'s
weighted solve) and slate_unknown.py (`frame`, `fit_tv`), with the beam built
as e2e_measurement/score.py `calibrations()` builds its label-free one; the
method is wuwnet-fishsense2026@5532b99 fishsense_wuwnet/laser.py
`close_with_apparent_size` (paper §4.3).

**Golden**: `fixtures/size_constancy_golden.json` holds, for the paper's ten
calibration sessions (Table 3), the pair equations cscw's own `pair_scale`
produced from its SIFT features, the dots, the dive line, the camera matrix
and the stored calibration's axis; and cscw's output (`labelfree_tv.csv`).
The port must reproduce that output -- the vanishing position to a
micro-pixel, and so Table 3: 9 of 10 sessions within 0.05 deg of the
known-size calibration, 10 of 10 within 0.15 deg.

The rest are synthetic, CPU-only: a beam the fit must recover, a texture
whose scale the registration must recover, and the refusals.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from fishsense_services_processor.automatic_results import size_constancy as sc

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "size_constancy_golden.json").read_text()
)["dives"]
#: cscw's nominal focal length for turning pixels into degrees (slate_unknown.F_PX).
F_PX = 2850.0


def _pairs(d):
    return [sc.PairScale(int(i), int(j), float(v), int(n)) for i, j, v, n in d["pairs"]]


def _stored_vanishing(d, u):
    """The stored (known-size) calibration's vanishing point, along the line."""
    k = np.array(d["camera_matrix"])
    ax = np.array(d["stored_axis"])
    v = np.array([k[0, 0] * ax[0] / ax[2] + k[0, 2], k[1, 1] * ax[1] / ax[2] + k[1, 2]])
    return float(v @ u)


# -- golden: cscw's labelfree_tv.csv, from cscw's own pair equations -----------------


@pytest.mark.parametrize("dive", sorted(GOLDEN, key=int))
def test_reproduces_cscws_label_free_fit(dive):
    d = GOLDEN[dive]
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


def test_bootstrap_reproduces_cscws_standard_errors():
    """cscw draws every session's bootstrap from one generator, in Table 3's
    order; so must this, to reproduce its SEs."""
    rng = np.random.default_rng(0)
    for dive in sorted(GOLDEN, key=int):
        d = GOLDEN[dive]
        dots = np.array(d["dots"])
        sizes = sc.solve_log_sizes(len(dots), _pairs(d))
        xy = dots[sizes.connected]
        t = xy @ sc.line_direction(d["line"], xy)
        se = sc.bootstrap_se(t, np.exp(sizes.log_sizes[sizes.connected]), rng=rng)
        assert se == pytest.approx(d["expected"]["se_px"], rel=1e-6), dive


def test_table_3_nine_of_ten_within_0_05_deg_and_all_within_0_15():
    errors = []
    for d in GOLDEN.values():
        fit = sc.fit_label_free(
            dots=np.array(d["dots"]),
            pairs=_pairs(d),
            camera_matrix=d["camera_matrix"],
            line=d["line"],
            min_frames=1,
            min_size_ratio=1.0,
        )
        u = np.array(fit.line_direction)
        errors.append(np.degrees((fit.vanishing_px - _stored_vanishing(d, u)) / F_PX))
    errors = np.abs(errors)
    assert (errors <= 0.05).sum() == 9
    assert (errors <= 0.15).all()
    assert np.median(errors) == pytest.approx(0.02, abs=0.01)


def test_the_beam_is_built_as_cscws_label_free_calibration():
    """score.py `calibrations()`: v = tv u + offset n; the axis is K^-1 [v, 1];
    the origin is |O| along the locus direction in normalised coordinates."""
    d = GOLDEN["71"]
    k = np.array(d["camera_matrix"])
    want = d["expected"]
    u = np.array([want["ux"], want["uy"]])
    n = np.array([-u[1], u[0]])
    v = want["tv_fit"] * u + want["line_offset"] * n
    axis = np.linalg.solve(k, [v[0], v[1], 1.0])
    o = np.array([u[0] / k[0, 0], u[1] / k[1, 1]])
    origin = np.append(0.104 * o / np.linalg.norm(o), 0.0)

    got_origin, got_axis = sc.beam_from_vanishing(
        want["tv_fit"], u, want["line_offset"], k
    )

    assert got_axis == pytest.approx(axis / np.linalg.norm(axis), abs=1e-12)
    assert got_origin == pytest.approx(origin, abs=1e-12)


# -- synthetic: a beam the fit must recover ----------------------------------------

K = np.array([[2850.0, 0.0, 2000.0], [0.0, 2850.0, 1500.0], [0.0, 0.0, 1.0]])
TRUE_O = np.append(0.104 * np.array([-0.29, -0.957]) / np.hypot(0.29, 0.957), 0.0)
TRUE_D = np.array([0.012, 0.035, 1.0])


def _dot_at(z):
    p = TRUE_O + (z - TRUE_O[2]) / TRUE_D[2] * TRUE_D
    q = K @ (p / p[2])
    return q[:2]


def _beam_angle(a, b):
    a, b = np.asarray(a) / np.linalg.norm(a), np.asarray(b) / np.linalg.norm(b)
    return float(np.degrees(np.arccos(np.clip(a @ b, -1, 1))))


def _exact_pairs(z):
    """Exact pairwise size ratios of an object 1/z in apparent size."""
    logs = -np.log(z)
    n = len(z)
    return [sc.PairScale(i, j, logs[j] - logs[i], 20)
            for i in range(n) for j in range(i + 1, n)]  # fmt: skip


def test_recovers_a_known_beam_from_apparent_size_alone():
    z = np.linspace(1.0, 3.0, 12)
    dots = np.array([_dot_at(zi) for zi in z])

    fit = sc.fit_label_free(
        dots=dots, pairs=_exact_pairs(z), camera_matrix=K, line=None
    )

    assert fit.outcome == "accepted", fit.refusal_reason
    assert _beam_angle(fit.laser_axis, TRUE_D) < 0.01
    assert np.linalg.norm(fit.laser_position) == pytest.approx(0.104)
    assert fit.laser_position[2] == 0.0
    assert fit.size_ratio == pytest.approx(3.0)
    assert fit.frames_used == 12


def test_a_wrong_object_size_does_not_move_the_beam():
    """Size constancy: the object's physical size cancels."""
    z = np.linspace(1.0, 3.0, 12)
    dots = np.array([_dot_at(zi) for zi in z])
    u = sc.line_direction(None, dots)
    t = dots @ u
    small = sc.fit_vanishing_position(t, 1.0 / z)
    large = sc.fit_vanishing_position(t, 7.0 / z)
    assert small == pytest.approx(large, abs=1e-3)
    assert (
        _beam_angle(
            sc.beam_from_vanishing(small, u, float(np.median(dots @ [-u[1], u[0]])), K)[
                1
            ],
            TRUE_D,
        )
        < 0.01
    )


def test_a_bad_pair_is_dropped_by_the_loop_check():
    """Pairs carry ~1 % scale noise (register's floor); one matched another
    layer and is 35 % off. The loop of the other pairs outvotes it."""
    z = np.linspace(1.0, 3.0, 12)
    dots = np.array([_dot_at(zi) for zi in z])
    rng = np.random.default_rng(3)
    clean = [sc.PairScale(p.i, p.j, p.log_scale + rng.normal(0, 0.01), p.near)
             for p in _exact_pairs(z)]  # fmt: skip
    bad = list(clean)
    bad[5] = sc.PairScale(bad[5].i, bad[5].j, bad[5].log_scale + 0.3, bad[5].near)

    want = sc.fit_label_free(dots=dots, pairs=clean, camera_matrix=K)
    got = sc.fit_label_free(dots=dots, pairs=bad, camera_matrix=K)

    assert got.vanishing_px == pytest.approx(want.vanishing_px, abs=0.5)
    assert _beam_angle(got.laser_axis, TRUE_D) < 0.05


# -- the refusals ----------------------------------------------------------------


def test_refuses_too_few_frames():
    z = np.linspace(1.0, 3.0, 5)
    dots = np.array([_dot_at(zi) for zi in z])
    fit = sc.fit_label_free(dots=dots, pairs=_exact_pairs(z), camera_matrix=K)
    assert (fit.outcome, fit.refusal_reason) == ("refused", "too_few_frames")
    assert fit.laser_axis is None and fit.laser_position is None


def test_refuses_too_little_range_spread():
    """Paper §4.3: the method needs about 1.5x in distance; sessions under
    1.12x are unconstrained."""
    z = np.linspace(2.0, 2.6, 12)
    dots = np.array([_dot_at(zi) for zi in z])
    fit = sc.fit_label_free(dots=dots, pairs=_exact_pairs(z), camera_matrix=K)
    assert (fit.outcome, fit.refusal_reason) == ("refused", "too_little_range_spread")
    assert fit.size_ratio == pytest.approx(1.3)


def test_refuses_frames_no_pair_connects():
    z = np.linspace(1.0, 3.0, 12)
    dots = np.array([_dot_at(zi) for zi in z])
    fit = sc.fit_label_free(dots=dots, pairs=[], camera_matrix=K)
    assert (fit.outcome, fit.refusal_reason) == ("refused", "too_few_matched_pairs")


def test_refuses_a_fit_pinned_at_the_search_bound():
    """Unconstrained: the optimum is the bracket's edge, not a minimum."""
    z = np.linspace(1.0, 3.0, 12)
    dots = np.array([_dot_at(zi) for zi in z])
    # Sizes that grow with range: no vanishing point explains them.
    pairs = _exact_pairs(1.0 / z)
    fit = sc.fit_label_free(dots=dots, pairs=pairs, camera_matrix=K)
    assert (fit.outcome, fit.refusal_reason) == ("refused", "unconstrained")


# -- registration: the scale between two frames ------------------------------------


def _texture(seed=0, size=(1600, 2200)):
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 255, size=(size[0] // 8, size[1] // 8), dtype=np.uint8)
    img = cv2.resize(noise, (size[1], size[0]), interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def test_registration_recovers_the_scale_about_the_dot():
    dot = np.array([1100.0, 800.0])
    a = _texture()
    scale = 1.25
    m = cv2.getRotationMatrix2D((float(dot[0]), float(dot[1])), 0.0, scale)
    b = cv2.warpAffine(a, m, (a.shape[1], a.shape[0]))

    fa = sc.extract_features(a, dot)
    fb = sc.extract_features(b, dot)
    got = sc.pair_scale(fa, fb, dot)

    assert got is not None
    log_scale, near = got
    assert log_scale == pytest.approx(np.log(scale), abs=0.01)
    assert near >= sc.MIN_PAIR_NEAR


def test_registration_needs_features():
    empty = sc.FrameFeatures(xy=np.zeros((0, 2), np.float32), desc=None)
    assert sc.pair_scale(empty, empty, np.array([0.0, 0.0])) is None


def _cscw_features() -> Path | None:
    """cscw's SIFT features (65 MB, not vendored), from a sibling checkout."""
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "cscw-fishsense2027/calibration_analysis/e1j_size_constancy/features"
        )
        if candidate.is_dir():
            return candidate
    return None


CSCW_FEATURES = _cscw_features()


@pytest.mark.skipif(CSCW_FEATURES is None, reason="needs ../cscw-fishsense2027")
@pytest.mark.parametrize("dive", ["63", "94"])
def test_registration_reproduces_cscws_pairs_from_its_features(dive):
    """The registration itself, against the golden pairs, when the sibling
    checkout is present."""
    d = GOLDEN[dive]
    features = []
    for image_id in d["image_ids"]:
        z = np.load(CSCW_FEATURES / f"{image_id}.npz", allow_pickle=True)
        features.append(sc.FrameFeatures(xy=z["xy"], desc=z["desc"]))

    got = sc.pair_scales(features, np.array(d["dots"]))

    assert [(p.i, p.j, p.near) for p in got] == [(i, j, n) for i, j, _, n in d["pairs"]]
    assert [p.log_scale for p in got] == pytest.approx([v for _, _, v, _ in d["pairs"]])
