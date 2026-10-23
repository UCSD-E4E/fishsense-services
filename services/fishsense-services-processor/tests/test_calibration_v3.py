"""Label-free calibration v3 (owner, 2026-10-22): the slates fit the beam, the
rest of the dive checks it. On synthetic dives with a known beam: the fit is
real (`size_constancy.fit_label_free` on exact pair ratios), the dots are the
beam's projections at known ranges."""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.automatic_calibration import v3
from fishsense_services_processor.automatic_results import size_constancy as sc

K = np.array([[2850.0, 0, 2007.0], [0, 2850.0, 1508.0], [0, 0, 1.0]])
O = np.array([-0.031, -0.099, 0.0])
A = np.array([0.0135, 0.0249, 0.9996]) / np.linalg.norm([0.0135, 0.0249, 0.9996])


def _dot(z):
    p = O + ((z - O[2]) / A[2]) * A
    q = K @ p
    return q[:2] / q[2]


#: 30 px off the beam's image, across it.
OFF = 30.0 * v3.beam_line(K, O, A)[:2]


def _slates(ranges):
    return np.array([_dot(z) for z in ranges])


def _fitter(slate_xy, ranges):
    """The real fit on exact size ratios (s ~ 1/Z): what features would give."""

    def fit(indices):
        idx = list(indices)
        z = np.asarray(ranges, float)[idx]
        pairs = [sc.PairScale(a, b, float(np.log(z[a] / z[b])), 20)
                 for a in range(len(idx)) for b in range(a + 1, len(idx))]  # fmt: skip
        return sc.fit_label_free(dots=np.asarray(slate_xy)[idx], pairs=pairs,
                                 camera_matrix=K, min_frames=v3.MIN_FRAMES)  # fmt: skip

    return fit


def _angle(a, b):
    return np.degrees(
        np.arccos(np.clip(np.dot(a, b) / np.linalg.norm(a) / np.linalg.norm(b), -1, 1))
    )


def _run(slate_xy, ranges, other=(), dive_line=None):
    return v3.calibrate_v3(slate_xy=slate_xy, dive_line=dive_line, other_xy=list(other),
                           camera_matrix=K, fit=_fitter(slate_xy, ranges))  # fmt: skip


def test_three_agreeing_slates_recover_the_beam():
    ranges = [0.8, 1.4, 2.4]
    r = _run(_slates(ranges), ranges)
    assert r.outcome == "accepted" and r.kept == [0, 1, 2]
    assert _angle(r.fit.laser_axis, A) < 0.05


def test_two_slates_are_too_few():
    ranges = [0.8, 2.4]
    r = _run(_slates(ranges), ranges)
    assert (r.outcome, r.refusal_reason) == ("refused", "too_few_frames")


def test_a_slate_dot_off_the_others_is_left_out():
    ranges = [0.8, 1.1, 1.5, 2.0, 2.6]
    xy = _slates(ranges)
    xy[2] += 40.0 / 30.0 * OFF  # a reflection 40 px off the beam
    r = _run(xy, ranges)
    assert r.outcome == "accepted" and 2 not in r.kept and r.consensus_dropped == 1


def test_the_dive_line_drops_only_gross_outliers():
    ranges = [0.8, 1.1, 1.5, 2.0]
    xy = np.vstack([_slates(ranges), [[300.0, 2900.0]]])  # a speck far from the beam
    line = v3._line_through(_slates([0.6, 5.0]))
    near = xy.copy()
    near[1] += OFF  # within the wide margin: left to the slates to judge
    assert v3.gross_screen(xy, line) == [0, 1, 2, 3]
    assert v3.gross_screen(near, line) == [0, 1, 2, 3]
    assert v3.gross_screen(xy, None) == [0, 1, 2, 3, 4]


def test_two_lines_of_slate_dots_are_ambiguous():
    ranges = [0.8, 1.0, 1.3, 1.7, 2.2, 2.8]
    xy = _slates(ranges)
    rival = xy[:4] + 2 * OFF  # a reflection's line, 60 px off, 4 dots to 6
    r = _run(np.vstack([xy, rival]), ranges + ranges[:4])
    assert (r.outcome, r.refusal_reason) == ("refused", "ambiguous_line")


def test_the_dive_checks_the_fit():
    ranges = [0.8, 1.4, 2.4]
    good = [_dot(z) for z in np.linspace(0.6, 4.0, 30)]
    assert _run(_slates(ranges), ranges, other=good).check.median_px < 1.0
    # A fit whose beam the dive disagrees with: every other dot 30 px off.
    off = [p + OFF for p in good]
    r = _run(_slates(ranges), ranges, other=off)
    assert (r.outcome, r.refusal_reason) == ("refused", "fit_disagrees_with_mission")
    assert r.check.dots == 30 and r.check.within_share == 0.0


def test_too_few_other_dots_do_not_decide():
    ranges = [0.8, 1.4, 2.4]
    off = [_dot(z) + OFF for z in np.linspace(0.6, 4.0, v3.CHECK_MIN_DOTS - 1)]
    r = _run(_slates(ranges), ranges, other=off)
    assert r.outcome == "accepted" and not r.check.decides


def test_most_other_dots_on_the_beam_accept_it():
    ranges = [0.8, 1.4, 2.4]
    dots = [_dot(z) for z in np.linspace(0.6, 4.0, 20)]
    dots[:8] = [p + OFF for p in dots[:8]]  # 40% off: those frames' problem
    r = _run(_slates(ranges), ranges, other=dots)
    assert r.outcome == "accepted" and r.check.within_share == pytest.approx(0.6)


# -- the replay: the activity's fit over extracted features, and its draws ----------

import uuid  # noqa: E402

from fishsense_services_contracts.automatic_results import (  # noqa: E402
    AutomaticCalibrationFrame,
    FitAutomaticCalibrationInput,
)
from fishsense_services_processor.automatic_calibration.activities import (  # noqa: E402
    replay_v3,
)


def _payload(ranges, other_ranges):
    slates = _slates(ranges)
    frames = [AutomaticCalibrationFrame(capture_id=uuid.uuid4(), jpeg_url="u",
                                        x=float(x), y=float(y)) for x, y in slates]  # fmt: skip
    others = [list(map(float, _dot(z))) for z in other_ranges]
    line_dots = [[f.x, f.y] for f in frames] + others
    return FitAutomaticCalibrationInput(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                                        camera_matrix=K.tolist(), frames=frames,
                                        line_dots=line_dots)  # fmt: skip


def _pairs_by_range(ranges):
    """A `pairs_of` seam: features are frame indices; ratios exact."""

    def pairs_of(features, dots):
        z = [ranges[i] for i in features]
        return [sc.PairScale(a, b, float(np.log(z[a] / z[b])), 20)
                for a in range(len(z)) for b in range(a + 1, len(z))]  # fmt: skip

    return pairs_of


def test_the_replay_fits_and_checks_and_draws():
    ranges = [0.7, 0.9, 1.2, 1.6, 2.1, 2.8]
    payload = _payload(ranges, np.linspace(0.6, 4.0, 25))
    report = replay_v3(payload, list(range(6)), payload.frames, draw_sizes=[3, 4, 9],
                       draws_per_size=5, pairs_of=_pairs_by_range(ranges))  # fmt: skip
    assert report.outcome == "accepted" and report.frames_used == 6
    assert _angle(report.laser_axis, A) < 0.05
    assert report.check_dots == 25 and report.check_median_px < 1.0
    assert [d.size for d in report.draws] == [3] * 5 + [4] * 5  # 9 > 6 frames: none
    assert all(d.outcome == "accepted" and _angle(d.laser_axis, A) < 0.05
               for d in report.draws)  # fmt: skip
    assert len({tuple(d.laser_axis) for d in report.draws}) >= 1


def test_the_slate_dots_are_not_their_own_check():
    ranges = [0.7, 1.2, 2.1]
    payload = _payload(ranges, [])
    report = replay_v3(payload, list(range(3)), payload.frames,
                       pairs_of=_pairs_by_range(ranges))  # fmt: skip
    assert report.check_dots == 0


def test_the_check_refuses_on_the_median_distance():
    ranges = [0.8, 1.4, 2.4]
    dots = [_dot(z) for z in np.linspace(0.6, 4.0, 20)]
    near = [p + OFF * (9.0 / 30.0) for p in dots]  # every dot 9 px off: kept
    far = [p + OFF * (11.0 / 30.0) for p in dots]  # every dot 11 px off: refused
    assert _run(_slates(ranges), ranges, other=near).outcome == "accepted"
    r = _run(_slates(ranges), ranges, other=far)
    assert r.refusal_reason == "fit_disagrees_with_mission"
    assert r.check.median_px == pytest.approx(11.0, abs=0.01)
