"""The automatic-results validation harness: the paper's metrics, pure.

New in v2. The scoring is cscw-fishsense2027@96a8da07's:
e2e_measurement/score.py (`depth`, `length`, `p90`, the stage ladder and its
per-fish p90 error against tape: Table 4) and e2e_measurement/tail/evaluate.py
(fully automatic vs manual length on the reef's calibrated dives, same stored
calibration: median, MAE, within 5/10 %, over 20 %; and coverage of the frames
humans measured). Pinned here on synthetic frames; the CLI runs it on a
database (test_validate_automatic_cli.py).
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_api.automatic_validation import (
    PAPER_POOL_DIVES,
    PAPER_REEF_CALIBRATED_DIVES,
    PAPER_REEF_DIVES,
    ValidationFrame,
    depth,
    fish_p90,
    length,
    pool_ladder,
    reef_comparison,
    reef_coverage,
)

K = np.array([[2850.0, 0.0, 2000.0], [0.0, 2850.0, 1500.0], [0.0, 0.0, 1.0]])
O = np.array([-0.03, -0.0996, 0.0])
A = np.array([0.012, 0.035, 1.0])


def _dot_at(z):
    p = O + z * A
    q = K @ (p / p[2])
    return (float(q[0]), float(q[1]))


def test_the_paper_dives():
    """run_e2e.py ORDER; tail/stage.py CALIBRATED_REEF plus the two green dives."""
    assert PAPER_POOL_DIVES == (58, 59, 60, 61, 66, 76, 84, 87, 94, 114)
    assert PAPER_REEF_CALIBRATED_DIVES == (347, 341, 465, 349, 279, 471, 436, 383)
    assert set(PAPER_REEF_DIVES) == {*PAPER_REEF_CALIBRATED_DIVES, 362, 366}


def test_depth_is_the_laser_points_z():
    x, y = _dot_at(2.5)
    assert depth(K, O, A, x, y) == pytest.approx(2.5)


def test_length_is_head_to_tail_on_the_plane_at_that_depth():
    # 285 px at 2 m with f = 2850 is 0.2 m.
    assert length(K, 2.0, 1000.0, 1500.0, 1285.0, 1500.0) == pytest.approx(0.2)


def test_fish_p90_is_productions_estimator():
    """score.p90: the ceil(0.9 n)-th smallest."""
    assert fish_p90([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 9
    assert fish_p90([3.0]) == 3.0


def _pool(z, true_length=0.4, model="Grouper", dive=58, **overrides):
    dot = _dot_at(z)
    half = true_length / 2 * K[0, 0] / z
    ht = (dot[0] - half, dot[1], dot[0] + half, dot[1])
    fields = dict(
        capture_number=int(z * 1000), dive_number=dive, set="pool", model=model,
        true_length_m=true_length, green=False, camera_matrix=K.tolist(),
        human_dot=dot, human_head_tail=ht, auto_dot=dot, auto_head_tail=ht,
        stored_calibration=(O.tolist(), A.tolist()),
        label_free_calibration=(O.tolist(), A.tolist()),
    )  # fmt: skip
    fields.update(overrides)
    return ValidationFrame(**fields)


def test_a_perfect_chain_scores_zero_everywhere():
    rows = {r.config: r for r in pool_ladder([_pool(z) for z in (1.0, 1.5, 2.0)])}

    assert set(rows) >= {"A", "B", "D", "E", "F"}
    for row in rows.values():
        assert row.coverage == 1.0
        assert row.fish_p90_mae == pytest.approx(0.0, abs=1e-9)


def test_the_ladder_isolates_each_stage():
    """A 10 % long automatic head/tail costs E and F 10 %, never A, B or D."""
    frames = []
    for z in (1.0, 1.5, 2.0):
        f = _pool(z)
        hx, hy, tx, ty = f.auto_head_tail
        cx = (hx + tx) / 2
        frames.append(_pool(z, auto_head_tail=(cx + (hx - cx) * 1.1, hy,
                                               cx + (tx - cx) * 1.1, ty)))  # fmt: skip

    rows = {r.config: r for r in pool_ladder(frames)}

    assert rows["A"].fish_p90_mae == pytest.approx(0.0, abs=1e-9)
    assert rows["B"].fish_p90_mae == pytest.approx(0.0, abs=1e-9)
    assert rows["E"].fish_p90_mae == pytest.approx(0.10, abs=1e-6)
    assert rows["F"].fish_p90_mae == pytest.approx(0.10, abs=1e-6)


def test_a_frame_with_no_automatic_head_tail_costs_coverage_not_error():
    frames = [_pool(1.0), _pool(2.0, auto_head_tail=None)]
    rows = {r.config: r for r in pool_ladder(frames)}
    assert rows["F"].coverage == 0.5 and rows["A"].coverage == 1.0
    assert rows["F"].fish_p90_mae == pytest.approx(0.0, abs=1e-9)


def test_per_fish_error_is_per_dive_and_model():
    frames = [_pool(1.0, dive=58), _pool(1.0, dive=59, model="Snook", true_length=0.5)]
    assert pool_ladder(frames)[0].fish_n == 2


def _reef(z, scale=1.0, green=False, auto=True):
    f = _pool(z)
    hx, hy, tx, ty = f.human_head_tail
    cx = (hx + tx) / 2
    auto_ht = (cx + (hx - cx) * scale, hy, cx + (tx - cx) * scale, ty)
    return ValidationFrame(**{
        **vars(f), "set": "reef", "green": green, "label_free_calibration": None,
        "model": None, "true_length_m": None,
        "auto_head_tail": auto_ht if auto else None,
    })  # fmt: skip


def test_reef_automatic_against_manual_length():
    frames = [_reef(1.0, 1.00), _reef(1.5, 1.04), _reef(2.0, 0.93), _reef(2.5, 1.25),
              _reef(3.0, auto=False)]  # fmt: skip

    r = reef_comparison(frames)

    assert r.frames == 5 and r.measured == 4
    assert r.median == pytest.approx(0.02)
    assert r.mae == pytest.approx((0 + 0.04 + 0.07 + 0.25) / 4)
    assert r.within_5 == pytest.approx(0.5)
    assert r.within_10 == pytest.approx(0.75)
    assert r.over_20 == pytest.approx(0.25)


def test_reef_lengths_need_a_stored_calibration():
    f = _reef(1.0)
    frames = [ValidationFrame(**{**vars(f), "stored_calibration": None})]
    assert reef_comparison(frames).frames == 0


def test_coverage_of_the_frames_humans_measured_by_laser_colour():
    frames = [_reef(1.0), _reef(1.5, auto=False), _reef(2.0, green=True),
              _reef(2.5, green=True, auto=False), _reef(3.0, green=True, auto=False)]  # fmt: skip

    c = reef_coverage(frames)

    assert c["all"] == pytest.approx(2 / 5)
    assert c["red"] == pytest.approx(1 / 2)
    assert c["green"] == pytest.approx(1 / 3)
