"""Label-free calibration v3 (owner, 2026-10-22): the slates fit the beam, the
rest of the dive checks it. On synthetic dives with a known beam: the fit is
real (`size_constancy.fit_label_free` on exact pair ratios), the dots are the
beam's projections at known ranges."""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_core import size_constancy as sc
from fishsense_core.line_fit import fit_line_pca

from fishsense_services_processor.automatic_calibration import pose, v3

K = np.array([[2850.0, 0, 2007.0], [0, 2850.0, 1508.0], [0, 0, 1.0]])
#: The housing's laser (calibration v4 casts its beam from it, 2026-10-27).
O = np.asarray(pose.HOUSING_ORIGIN_M, float)
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
    line = np.array(fit_line_pca(_slates([0.6, 5.0])))
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


# -- strategy 2 (owner, 2026-10-22): sizes from SAM masks, no pair registration ------


def _sizes(ranges):
    return [100.0 / z for z in ranges]  # a rigid slate: s ~ 1/Z, any units


def test_known_sizes_recover_the_beam_without_pairs():
    ranges = [0.8, 1.2, 1.7, 2.4]
    f = v3.fit_known_sizes(_slates(ranges), _sizes(ranges), K, scale_sd_max=np.inf)
    assert f.outcome == "accepted" and f.pairs == 0 and f.frames_used == 4
    assert _angle(f.laser_axis, A) < 0.05


def test_a_frame_with_no_mask_is_left_out():
    ranges = [0.8, 1.2, 1.7, 2.4]
    sizes = _sizes(ranges)
    sizes[1] = None
    f = v3.fit_known_sizes(_slates(ranges), sizes, K, scale_sd_max=np.inf)
    assert f.outcome == "accepted" and f.used == [0, 2, 3]


def test_a_size_that_disagrees_is_trimmed():
    ranges = [0.8, 1.0, 1.3, 1.7, 2.2, 2.8]
    sizes = _sizes(ranges)
    sizes[3] *= 1.4  # SAM took the slate and the hand holding it
    f = v3.fit_known_sizes(_slates(ranges), sizes, K, scale_sd_max=np.inf)
    assert f.outcome == "accepted" and 3 not in f.used
    assert _angle(f.laser_axis, A) < 0.05


def test_too_little_spread_and_too_few_are_refused():
    near = [1.0, 1.05, 1.1]
    assert v3.fit_known_sizes(_slates(near), _sizes(near), K).refusal_reason == (
        "too_little_range_spread"
    )
    two = [0.8, 2.4]
    assert (
        v3.fit_known_sizes(_slates(two), _sizes(two), K).refusal_reason
        == "too_few_frames"
    )


def test_v3_runs_on_known_sizes():
    ranges = [0.8, 1.4, 2.4]
    xy, s = _slates(ranges), _sizes(ranges)
    r = v3.calibrate_v3(slate_xy=xy, dive_line=None, other_xy=[], camera_matrix=K,
                        fit=lambda idx: v3.fit_known_sizes(xy[list(idx)],
                                                           [s[i] for i in idx], K,
                                                           scale_sd_max=np.inf))  # fmt: skip
    assert r.outcome == "accepted" and _angle(r.fit.laser_axis, A) < 0.05


def test_the_replay_fits_on_sam_sizes_without_pairs():
    ranges = [0.7, 1.2, 2.1, 2.8]
    payload = _payload(ranges, np.linspace(0.6, 4.0, 20))

    def no_pairs(features, dots):
        raise AssertionError("no pair registration on SAM sizes")

    sizes = _sizes(ranges)
    sizes[0] = None
    report = replay_v3(payload, [], payload.frames, sizes=sizes, draw_sizes=[3],
                       draws_per_size=4, pairs_of=no_pairs)  # fmt: skip
    assert report.size_source == "sam" and report.outcome == "accepted"
    assert report.frames_used == 3 and _angle(report.laser_axis, A) < 0.05
    assert len(report.draws) == 4
    # The replay gates on nothing: it records the prediction, for the draws.
    assert report.scale_sd is not None
    assert all(d.scale_sd is not None for d in report.draws if d.outcome == "accepted")


# -- noise-aware trim and the uncertainty gate (2026-10-23) ---------------------------


def test_near_widely_spread_slates_predict_the_smallest_scale_error():
    wide = [0.4, 0.6, 0.9, 1.4, 2.2, 3.0]
    f = v3.fit_known_sizes(_slates(wide), _sizes(wide), K)
    assert f.outcome == "accepted" and f.scale_sd < v3.SCALE_SD_MAX
    narrow = [0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]
    g = v3.fit_known_sizes(_slates(narrow), _sizes(narrow), K, scale_sd_max=np.inf)
    assert g.scale_sd > f.scale_sd


def test_far_slates_near_the_vanishing_point_are_too_uncertain():
    """1 px along the line is several percent of range when the slates are
    far: the fit says so and is refused rather than trusted."""
    ranges = [6.0, 8.0, 11.0]
    f = v3.fit_known_sizes(_slates(ranges), _sizes(ranges), K)
    assert (f.outcome, f.refusal_reason) == ("refused", "uncertain_fit")
    assert f.scale_sd > v3.SCALE_SD_MAX


def test_a_dot_off_along_the_line_is_trimmed_by_what_noise_explains():
    """A dot a few px off along the line keeps its size: its residual is a few
    percent -- under the fixed 10% trim, far over what 1 px of dot noise
    explains at a near range."""
    ranges = [0.6, 0.75, 0.9, 1.1, 1.35, 1.7]
    xy = _slates(ranges)
    u = (xy[-1] - xy[0]) / np.linalg.norm(xy[-1] - xy[0])
    xy[2] += 25.0 * u  # along the beam, so no consensus line drops it
    f = v3.fit_known_sizes(xy, _sizes(ranges), K, scale_sd_max=np.inf)
    assert f.outcome == "accepted" and 2 not in f.used
    assert _angle(f.laser_axis, A) < 0.05


# -- calibration v4 (2026-10-23): points on automatically posed slates ---------------


def _points(ranges):
    return [list(O + ((z - O[2]) / A[2]) * A) for z in ranges]


def test_the_replay_fits_a_beam_through_pose_points():
    """No sizes, no pairs, no vanishing point: the beam through each slate
    frame's dot on its plane, checked by the dive as v3's."""
    ranges = [0.7, 1.2, 1.7, 2.1, 2.8]
    payload = _payload(ranges, np.linspace(0.6, 4.0, 20))

    def no_pairs(features, dots):
        raise AssertionError("no pair registration on pose points")

    points = _points(ranges)
    points[0] = None
    report = replay_v3(payload, [], payload.frames, points=points, draw_sizes=[3],
                       draws_per_size=10, pairs_of=no_pairs)  # fmt: skip
    assert report.size_source == "pose" and report.outcome == "accepted"
    assert report.frames_used == 4 and _angle(report.laser_axis, A) < 0.01
    # cast from the housing's laser (2026-10-27), 1.5 mm from this beam's
    assert np.allclose(report.laser_position, pose.HOUSING_ORIGIN_M, atol=1e-9)
    assert report.check_dots == 20 and report.check_median_px < 0.5
    # each draw fits its own frames' points (a draw without frame 0 has 3)
    assert len(report.draws) == 10
    assert any(d.outcome == "accepted" and d.frames_used == 3 for d in report.draws)


def test_pose_points_close_in_range_are_an_answer_with_their_bar_at_each_range():
    """From the housing's laser the beam needs no lever arm (2026-10-27: the
    free line refused 13 missions under 0.6 m); the report gives the bar at
    each range, which a length takes at its own depth."""
    ranges = [1.0, 1.1, 1.2, 1.3]
    payload = _payload(ranges, [])
    report = replay_v3(payload, [], payload.frames, points=_points(ranges))
    assert report.outcome == "accepted"
    assert [z for z, _ in report.scale_sd_by_range] == list(pose.SD_RANGES_M)
    assert report.scale_sd == max(
        sd for z, sd in report.scale_sd_by_range if 0.75 <= z <= 3.0
    )


def test_two_posed_slates_are_an_answer_with_an_error_bar():
    """Mission 471 (2026-10-26): 4 slate frames, 2 posed -- refused
    ``too_few_frames`` by v4, +0.3% against its human calibration relaxed.
    The answer carries its error bar."""
    ranges = [1.1, 1.6, 2.2, 2.7]
    payload = _payload(ranges, [])
    points = _points(ranges)
    points[1] = points[2] = None
    report = replay_v3(payload, [], payload.frames, points=points)
    assert report.outcome == "accepted" and report.frames_used == 2
    assert report.scale_sd >= pose.SCALE_SD_FLOOR


def test_two_slate_frames_are_enough_on_pose_points_only():
    ranges = [1.0, 2.4]
    payload = _payload(ranges, [])
    posed = replay_v3(payload, [], payload.frames, points=_points(ranges))
    assert posed.outcome == "accepted"
    sized = replay_v3(payload, [], payload.frames, sizes=_sizes(ranges))
    assert (sized.outcome, sized.refusal_reason) == ("refused", "too_few_frames")


def test_one_posed_slate_is_no_answer():
    ranges = [1.0, 1.7, 2.4]
    payload = _payload(ranges, [])
    points = _points(ranges)
    points[0] = points[2] = None
    report = replay_v3(payload, [], payload.frames, points=points)
    assert (report.outcome, report.refusal_reason) == ("refused", "too_few_frames")
    assert report.scale_sd is None and report.scale_sd_by_range is None


async def test_the_activity_maps_pose_points_by_capture_and_reads_no_image():
    """Frame 2's dot is off the others' line, so v3's consensus drops it,
    and with it its (wrong) point: only a point mapped to its own capture
    goes with its frame."""
    from temporalio.testing import (
        ActivityEnvironment,
    )  # pylint: disable=import-outside-toplevel

    from fishsense_services_contracts.calibration_v3 import ReplayCalibrationInput
    from fishsense_services_processor.automatic_calibration.activities import (
        AutomaticCalibrationActivities,
    )

    ranges = [0.7, 1.2, 1.7, 2.8]
    payload = _payload(ranges, [])
    f2 = payload.frames[2]
    payload.frames[2] = f2.model_copy(update={"x": f2.x + OFF[0], "y": f2.y + OFF[1]})
    payload.line_dots[2] = [payload.frames[2].x, payload.frames[2].y]
    pts = _points(ranges)
    pts[2] = list(np.array(pts[2]) + [0.08, 0.0, 0.0])
    points = dict(zip((str(f.capture_id) for f in payload.frames), pts))
    points = dict(reversed(points.items()))  # by capture id, not by order

    def no_store():
        raise AssertionError("no image is read for pose points")

    acts = AutomaticCalibrationActivities(store_factory=no_store)
    report = await ActivityEnvironment().run(
        acts.replay_automatic_calibration_v3,
        ReplayCalibrationInput(payload=payload, size_source="pose", pose_points=points),
    )
    assert report.outcome == "accepted" and report.consensus_dropped == 1
    assert report.frames_used == 3 and _angle(report.laser_axis, A) < 0.01
