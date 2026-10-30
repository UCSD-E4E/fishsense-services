"""Calibration v4's pieces against synthetic slates with known answers: the
board found from its mask, the printed pattern aligned either way up, the
plane and the dot's point it gives, and the beam from the housing's laser
through the points, with its error bar at each range."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from fishsense_services_processor.automatic_calibration import pose

K = np.array([[1500.0, 0, 1000], [0, 1500.0, 750], [0, 0, 1]])
SHAPE = (1500, 2000)
BOARD_M = 12 * 0.0254
DPI = 60
PAGE_IN = (8.5, 11.0)  # portrait, as the template PDFs are


def _pose(tilt_deg=20.0, spin_deg=0.0, z=1.2):
    r = cv2.Rodrigues(np.array([np.radians(tilt_deg), 0.0, 0.0]))[0]
    r = cv2.Rodrigues(np.array([0.0, 0.0, np.radians(spin_deg)]))[0] @ r
    centre = np.array([BOARD_M / 2, BOARD_M * pose.ASPECT / 2, 0.0])
    tv = np.array([0.03, -0.02, z]) - r @ centre
    return cv2.Rodrigues(r)[0].ravel(), tv


def _mask(rv, tv):
    poly = pose._project(
        pose._outline(pose.ASPECT, pose.RADIUS, 12) * BOARD_M, rv, tv, K
    )
    m = np.zeros(SHAPE, np.uint8)
    cv2.fillPoly(m, [np.int32(np.round(poly))], 1)
    return m.astype(bool)


def _page():
    """A page with bold, asymmetric marks; their centers are the reference
    points (template px at DPI)."""
    w, h = int(PAGE_IN[0] * DPI), int(PAGE_IN[1] * DPI)
    page = np.full((h, w), 255, np.uint8)
    refs = [(110, 120), (390, 150), (250, 330), (120, 560), (400, 590)]
    cv2.rectangle(page, (80, 90), (140, 150), 0, -1)
    cv2.circle(page, (390, 150), 38, 0, -1)
    cv2.line(page, (190, 330), (310, 330), 0, 22)
    cv2.line(page, (250, 270), (250, 390), 0, 22)
    cv2.fillPoly(page, [np.int32([(120, 520), (160, 600), (80, 600)])], 0)
    cv2.rectangle(page, (360, 560), (440, 620), 0, 8)
    cv2.rectangle(page, (200, 60), (300, 90), 0, -1)
    return page, np.array(refs, float)


def _template_to_body(page_shape):
    """Template px -> board meters: the page's long side along the board's,
    centered (as the slates are printed and mounted)."""
    ph, pw = page_shape
    s = 0.0254 / DPI
    # rotate 90 deg clockwise: (x, y) -> (ph - 1 - y, x), then center
    rot = np.array([[0, -1, ph - 1], [1, 0, 0], [0, 0, 1]], float)
    off = np.array([(BOARD_M - ph * s) / 2, (BOARD_M * pose.ASPECT - pw * s) / 2])
    return np.array([[s, 0, off[0]], [0, s, off[1]], [0, 0, 1]]) @ rot


def _h_body_to_image(rv, tv):
    r = cv2.Rodrigues(np.asarray(rv, float))[0]
    return K @ np.column_stack([r[:, 0], r[:, 1], tv])


def _render(rv, tv):
    page, refs = _page()
    h = _h_body_to_image(rv, tv) @ _template_to_body(page.shape)
    img = np.full(SHAPE, 40, np.uint8)
    img[_mask(rv, tv)] = 245
    ink = cv2.warpPerspective(255 - page, h, SHAPE[::-1], flags=cv2.INTER_AREA)
    img = np.clip(img.astype(int) - ink, 0, 255).astype(np.uint8)
    truth = cv2.perspectiveTransform(refs.reshape(-1, 1, 2), h).reshape(-1, 2)
    corners = pose._project(pose._outline(pose.ASPECT, 0.0) * BOARD_M, rv, tv, K)
    return img, page, refs, truth, corners


# -- the board --------------------------------------------------------------------------


@pytest.mark.parametrize("tilt", [5.0, 30.0])
def test_the_board_is_found_from_its_mask(tilt):
    rv, tv = _pose(tilt)
    fit = pose.fit_board(_mask(rv, tv), K)
    truth = pose._project(pose._outline(pose.ASPECT, 0.0) * BOARD_M, rv, tv, K)
    assert fit is not None and fit.plausible
    # the same corners (in some order) within a couple of pixels
    err = [min(np.linalg.norm(fit.corners - c, axis=1)) for c in truth]
    assert max(err) < 3.0, err
    assert abs(pose.tilt_deg(fit.rvec) - tilt) < 4.0


def test_an_empty_mask_has_no_board():
    assert pose.fit_board(np.zeros(SHAPE, bool), K) is None


# -- the pattern ------------------------------------------------------------------------


@pytest.mark.parametrize("spin,turn", [(0.0, 0), (0.0, 2), (180.0, 0)])
def test_the_pattern_is_aligned_either_way_up(spin, turn):
    """The board's corners say nothing of which way up the page is (the
    board fit may start from any corner): both are tried, and the reference
    points land on the marks."""
    rv, tv = _pose(15.0, spin)
    img, page, refs, truth, corners = _render(rv, tv)
    corners = np.roll(corners, turn, axis=0)
    h, cc = pose.align_template(img, corners, page, DPI)
    assert h is not None and cc > pose.ECC_MIN
    got = cv2.perspectiveTransform(refs.reshape(-1, 1, 2), h).reshape(-1, 2)
    assert np.max(np.linalg.norm(got - truth, axis=1)) < 1.5


def _aligned(spin, turn):
    rv, tv = _pose(15.0, spin)
    img, page, refs, truth, corners = _render(rv, tv)
    return (
        pose.align_template_both(img, np.roll(corners, turn, axis=0), page, DPI),
        refs,
        truth,
    )


@pytest.mark.parametrize("turn", [0, 1, 2, 3])
def test_both_orientations_are_scored_and_the_page_s_up_is_recorded(turn):
    """The 436 failure (2026-10-25): on a near-symmetric pattern v4 chose the
    wrong way up on 17 of 445 human-labeled frames, and nothing it recorded
    told them apart. The losing orientation's correlation is the margin a
    check needs; the page's up, in the image, is the orientation in the
    world -- whichever corner the board fit starts from (`turn`), and half a
    turn of the slate moves it half a turn."""
    upright, refs, truth = _aligned(0.0, turn)
    spun, _, _ = _aligned(180.0, turn)

    got = cv2.perspectiveTransform(refs.reshape(-1, 1, 2), upright.h).reshape(-1, 2)
    assert np.max(np.linalg.norm(got - truth, axis=1)) < 1.5
    for a in (upright, spun):
        assert a.ecc > pose.ECC_MIN
        assert a.ecc_other is not None and a.ecc_other < a.ecc - 0.05
    turned = (spun.page_up_deg - upright.page_up_deg) % 360.0
    assert abs(turned - 180.0) < 20.0
    assert abs(upright.page_up_deg - _aligned(0.0, 0)[0].page_up_deg) < 5.0


def test_a_blank_board_is_not_aligned_with_confidence():
    rv, tv = _pose(15.0)
    _, page, _, _, corners = _render(rv, tv)
    blank = np.full(SHAPE, 40, np.uint8)
    blank[_mask(rv, tv)] = 245
    _, cc = pose.align_template(blank, corners, page, DPI)
    assert cc is None or cc < pose.ECC_MIN


# -- the plane and the point ------------------------------------------------------------


def test_the_plane_and_the_dot_point_are_recovered():
    rv, tv = _pose(25.0)
    page, refs = _page()
    h = _h_body_to_image(rv, tv) @ _template_to_body(page.shape)
    plane, _, rms = pose.slate_plane(refs, DPI, h, K)
    r = cv2.Rodrigues(rv)[0]
    assert abs(abs(plane.normal @ r[:, 2]) - 1) < 1e-6 and rms < 0.01
    # a laser point on the board, seen at its pixel, meets the plane there
    target = r @ np.array([0.1, 0.08, 0.0]) + tv
    dot = (K @ target)[:2] / target[2]
    assert np.allclose(pose.dot_point(plane, dot, K), target, atol=1e-6)


def test_the_plane_and_its_rms_come_from_one_solve(monkeypatch):
    """One `solvePnP` gives both the plane and the rms that describes it. Two
    solves cost twice, and saw different inputs (core casts the body to
    float32), so the rms could describe a slightly different pose from the
    plane returned (review of #350)."""
    rv, tv = _pose(25.0)
    page, refs = _page()
    h = _h_body_to_image(rv, tv) @ _template_to_body(page.shape)
    calls = []
    real = cv2.solvePnP

    def solve(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(cv2, "solvePnP", solve)

    assert pose.slate_plane(refs, DPI, h, K) is not None
    assert len(calls) == 1


@pytest.mark.parametrize("h", [np.zeros((3, 3)), np.full((3, 3), np.nan)])
def test_a_degenerate_alignment_gives_no_plane(h):
    """(OpenCV places points a NaN homography sends nowhere at 0, 0.)"""
    _, refs = _page()
    assert pose.slate_plane(refs, DPI, h, K) is None


# -- the beam (2026-10-27: from the housing's laser) ------------------------------------

ORIGIN = np.asarray(pose.HOUSING_ORIGIN_M, float)
AXIS = np.array([-0.04, 0.01, 1.0]) / np.linalg.norm([-0.04, 0.01, 1.0])
#: A beam the housing's laser does not cast: the line 279's two wrong dots made.
STRAY = (ORIGIN + np.array([-0.08, -0.32, 0.0]), AXIS)


def _beam_points(zs, outlier=None, beam=(ORIGIN, AXIS)):
    o, a = beam
    pts = [o + (z / a[2]) * a for z in zs]
    if outlier is not None:
        pts[outlier] = pts[outlier] + np.array([0.08, 0.0, 0.0])  # 5% at 1.5 m
    return pts


def _burst(point, n, spread=0.002):
    """`n` frames of one slate hold: the same point, a few mm apart."""
    rng = np.random.default_rng(n)
    return [point + rng.normal(0, spread, 3) * [1, 1, 0] for _ in range(n)]


def test_the_beam_is_cast_from_the_housing_s_laser():
    fit = pose.fit_pose_points(_beam_points([0.6, 1.2, 1.8]))
    assert fit.outcome == "accepted" and fit.used == [0, 1, 2]
    assert np.allclose(fit.laser_position, ORIGIN, atol=1e-9)
    assert np.allclose(fit.laser_axis, AXIS, atol=1e-6)


def test_an_outlier_is_left_out():
    fit = pose.fit_pose_points(_beam_points([0.6, 0.9, 1.2, 1.5, 1.8, 2.1], outlier=3))
    assert fit.outcome == "accepted" and fit.used == [0, 1, 2, 4, 5]
    assert np.allclose(fit.laser_axis, AXIS, atol=1e-6)


def test_wrong_dots_that_agree_with_each_other_do_not_tilt_the_beam():
    """Mission 279 (2026-10-27): two of its five slates had a wrong dot, both
    on one line the housing's laser cannot cast; a free line through all
    five read +63% at 0.75 m. From the housing, they agree with no slate."""
    pts = _beam_points([1.35, 2.85, 2.0]) + _beam_points([0.96, 1.02], beam=STRAY)
    fit = pose.fit_pose_points(pts)
    assert fit.outcome == "accepted" and fit.used == [0, 1, 2]
    assert np.allclose(fit.laser_axis, AXIS, atol=1e-6)


def test_a_burst_of_frames_is_one_vote():
    """Frames of one slate hold are one observation (half of all posed points
    have another within 10 mm, 2026-10-27): three wrong frames of one hold
    lose to two right slates."""
    wrong = STRAY[0] + (1.4 / STRAY[1][2]) * STRAY[1]
    pts = _burst(wrong, 3) + _beam_points([1.2, 2.4])
    fit = pose.fit_pose_points(pts)
    assert fit.outcome == "accepted" and fit.used == [3, 4]


def test_frames_without_a_point_are_skipped_but_keep_their_index():
    pts = _beam_points([0.6, 1.0, 1.4, 1.8])
    pts.insert(1, None)
    fit = pose.fit_pose_points(pts)
    assert fit.outcome == "accepted" and fit.used == [0, 2, 3, 4]


def test_points_close_in_range_are_an_answer():
    """The housing fixes the beam's origin, so its direction needs no lever
    arm (the free line refused under 0.6 m: 13 missions, 2026-10-27)."""
    fit = pose.fit_pose_points(_beam_points([1.0, 1.1, 1.2, 1.3]))
    assert fit.outcome == "accepted"
    assert np.allclose(fit.laser_axis, AXIS, atol=1e-6)


def test_one_point_is_no_answer():
    fit = pose.fit_pose_points(_beam_points([1.2]) + [None])
    assert (fit.outcome, fit.refusal_reason) == ("refused", "too_few_frames")


def test_one_slate_hold_is_no_answer():
    fit = pose.fit_pose_points(_burst(_beam_points([1.2])[0], 4))
    assert (fit.outcome, fit.refusal_reason) == ("refused", "slates_disagree")


def test_two_points_are_an_answer():
    """Relaxed (the owner, 2026-10-26): two posed slates fix a beam; that
    they are few widens the error bar rather than refusing. Mission 471: two
    posed frames, +0.3% against its human calibration."""
    fit = pose.fit_pose_points(_beam_points([0.6, 1.8]) + [None])
    assert fit.outcome == "accepted" and fit.used == [0, 1]
    assert np.allclose(fit.laser_axis, AXIS, atol=1e-6)


def test_slates_that_agree_with_none_other_are_no_answer():
    pts = _beam_points([1.0]) + _beam_points([2.0], beam=STRAY)
    fit = pose.fit_pose_points(pts)
    assert (fit.outcome, fit.refusal_reason) == ("refused", "slates_disagree")


def test_two_lines_with_equal_support_are_no_answer():
    tilted = (ORIGIN, np.array([0.2, 0.01, 1.0]) / np.linalg.norm([0.2, 0.01, 1.0]))
    pts = _beam_points([1.0, 2.0]) + _beam_points([1.2, 2.2], beam=tilted)
    fit = pose.fit_pose_points(pts)
    assert (fit.outcome, fit.refusal_reason) == ("refused", "ambiguous_line")


def test_a_fit_without_a_camera_has_no_error_bar():
    fit = pose.fit_pose_points(_beam_points([0.6, 1.2, 1.8]))
    assert fit.scale_sd is None and fit.scale_sd_by_range is None


# -- a scan larger than the board (the V-Slates, 2026-10-23) -----------------------------

#: The V-Slate templates are 8.5 x 14 in scans of the physical slate, with the
#: 12 in board starting ~1.9 in from the page's left edge: centering the page on
#: the board starts the alignment ~1 in off, and a near-symmetric V then lets
#: it settle the wrong way round (mission 436).
SCAN_IN = (14.0, 8.5)
BOARD_AT_IN = 1.9


def _scan():
    w, h = int(SCAN_IN[0] * DPI), int(SCAN_IN[1] * DPI)
    page = np.full((h, w), 255, np.uint8)
    page[:, : int(0.6 * DPI)] = 200  # the scanner bed beside the board
    x0 = int(BOARD_AT_IN * DPI)
    v = np.int32([(x0 + 80, 120), (x0 + 330, 430), (x0 + 600, 90)])
    cv2.polylines(page, [v], False, 0, 45)
    cv2.putText(page, "2", (x0 + 20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 3)
    refs = np.array([(x0 + 80, 120), (x0 + 330, 430), (x0 + 600, 90),
                     (x0 + 200, 270), (x0 + 470, 260), (x0 + 330, 360)], float)  # fmt: skip
    return page, refs


def _render_scan(rv, tv):
    page, refs = _scan()
    s = 0.0254 / DPI
    to_body = np.array([[s, 0, -BOARD_AT_IN * 0.0254], [0, s, 0], [0, 0, 1]])
    h = _h_body_to_image(rv, tv) @ to_body
    img = np.full(SHAPE, 40, np.uint8)
    board = _mask(rv, tv)
    img[board] = 245
    ink = cv2.warpPerspective(255 - page, h, SHAPE[::-1], flags=cv2.INTER_AREA)
    ink[~board] = 0  # only the board is there to see
    img = np.clip(img.astype(int) - ink, 0, 255).astype(np.uint8)
    truth = cv2.perspectiveTransform(refs.reshape(-1, 1, 2), h).reshape(-1, 2)
    corners = pose._project(pose._outline(pose.ASPECT, 0.0) * BOARD_M, rv, tv, K)
    return img, page, refs, truth, corners


@pytest.mark.parametrize("spin,turn", [(0.0, 0), (0.0, 2), (180.0, 0)])
def test_a_scan_larger_than_the_board_is_found_where_it_sits(spin, turn):
    rv, tv = _pose(15.0, spin)
    img, page, refs, truth, corners = _render_scan(rv, tv)
    h, cc = pose.align_template(img, np.roll(corners, turn, axis=0), page, DPI)
    assert h is not None
    got = cv2.perspectiveTransform(refs.reshape(-1, 1, 2), h).reshape(-1, 2)
    assert np.max(np.linalg.norm(got - truth, axis=1)) < 1.5


# -- the error bar (the owner, 2026-10-26: uncertainty, not refusal) ---------------------

CAM = np.array([[2850.0, 0, 2007.0], [0, 2850.0, 1508.0], [0, 0, 1.0]])
RANGES = np.asarray(pose.SD_RANGES_M)


def _error(fit, beam=(ORIGIN, AXIS)):
    """The fit's depth error at each of `SD_RANGES_M` where the true beam's
    dot is seen (+0.01 = 1% far, so lengths 1% long)."""
    return pose.depth_error(CAM, beam, (fit.laser_position, fit.laser_axis))


def test_a_beam_against_itself_has_no_error():
    assert np.max(np.abs(pose.depth_error(CAM, (ORIGIN, AXIS), (ORIGIN, AXIS)))) < 1e-12


def test_the_bar_is_given_at_each_range():
    fit = pose.fit_pose_points(
        _beam_points(np.linspace(0.6, 3.0, 12)), camera_matrix=CAM
    )
    got = np.asarray(fit.scale_sd_by_range)
    assert np.allclose(got[:, 0], RANGES)
    assert np.all(got[:, 1] >= pose.SCALE_SD_FLOOR)
    fish = (got[:, 0] >= 0.75) & (got[:, 0] <= 3.0)
    assert fit.scale_sd == pytest.approx(got[fish, 1].max())


def test_fewer_and_closer_slates_carry_a_wider_bar():
    wide = pose.fit_pose_points(
        _beam_points(np.linspace(0.6, 3.0, 12)), camera_matrix=CAM
    )
    few = pose.fit_pose_points(_beam_points([1.0, 1.1]), camera_matrix=CAM)
    assert few.scale_sd > wide.scale_sd


def test_scatter_among_the_slates_widens_the_bar():
    """Slates that disagree (5% range errors, three times the pose noise)
    widen the bar well past what the noise model gives the same geometry:
    the bootstrap sees them."""
    zs = np.linspace(0.7, 2.8, 8)
    clean = pose.fit_pose_points(_beam_points(zs), camera_matrix=CAM)
    rng = np.random.default_rng(3)
    noisy = [p * (1 + rng.normal(0, 0.05)) for p in _beam_points(zs)]
    rough = pose.fit_pose_points(noisy, camera_matrix=CAM)
    assert rough.outcome == "accepted" and rough.scale_sd > 1.5 * clean.scale_sd


def test_repeating_each_frame_does_not_narrow_the_bar():
    """The bootstrap resamples slate holds, not frames, and the noise moves a
    hold as one: five frames of each hold are no more evidence than one
    (frames resampled, 279's two holds read as five independent slates).
    Slates 10% apart, so the bootstrap, not the floor, sets the bar."""
    rng = np.random.default_rng(5)
    pts = [
        p * (1 + rng.normal(0, 0.10)) for p in _beam_points(np.linspace(0.8, 2.6, 5))
    ]
    once = pose.fit_pose_points(pts, camera_matrix=CAM)
    five = pose.fit_pose_points(
        [q for p in pts for q in _burst(p, 5, 0.0005)], camera_matrix=CAM
    )
    assert five.scale_sd >= 0.9 * once.scale_sd


def test_repeating_a_hold_does_not_narrow_the_noise_model():
    """With two holds the bootstrap has nothing to resample and the noise
    model sets the bar: it moves a hold as one, so eight frames of each are
    the bar of one."""
    pts = _beam_points([1.0, 1.4])
    one = pose.fit_pose_points(pts, camera_matrix=CAM)
    eight = pose.fit_pose_points(
        [q for p in pts for q in _burst(p, 8, 0.0005)], camera_matrix=CAM
    )
    got = np.asarray(eight.scale_sd_by_range)[:, 1]
    assert np.all(got >= 0.9 * np.asarray(one.scale_sd_by_range)[:, 1])


def test_the_bar_covers_the_error_its_noise_model_makes():
    """Control with a known answer: a housing whose laser sits off the
    nominal by its spread, slates read at their ranges with the pose noise;
    at every range the true depth falls within one bar at least 68% of the
    time and within two at least 95%."""
    rng = np.random.default_rng(7)
    z1 = z2 = n = 0
    for _ in range(80):
        true = (ORIGIN + np.r_[rng.normal(0, pose.HOUSING_ORIGIN_SD_M, 2), 0.0], AXIS)
        zs = np.sort(rng.uniform(0.8, 3.2, rng.integers(2, 9)))
        pts = [
            p * (1 + rng.normal(0, pose.POSE_RANGE_SD))
            for p in _beam_points(zs, beam=true)
        ]
        fit = pose.fit_pose_points(pts, camera_matrix=CAM)
        if fit.outcome != "accepted":
            continue
        z = np.abs(_error(fit, true)) / np.asarray(fit.scale_sd_by_range)[:, 1]
        n += len(z)
        z1 += int(np.sum(z <= 1))
        z2 += int(np.sum(z <= 2))
    assert n >= 70 * len(RANGES) and z1 / n >= 0.68 and z2 / n >= 0.95
