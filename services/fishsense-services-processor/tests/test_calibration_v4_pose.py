"""Calibration v4's pieces against synthetic slates with known answers: the
board found from its mask, the printed pattern aligned either way up, the
plane and the dot's point it gives, and the beam fitted through the points
with an outlier trimmed and the lever-arm gate kept."""

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
    """A page with bold, asymmetric marks; their centres are the reference
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
    """Template px -> board metres: the page's long side along the board's,
    centred (as the slates are printed and mounted)."""
    ph, pw = page_shape
    s = 0.0254 / DPI
    # rotate 90 deg clockwise: (x, y) -> (ph - 1 - y, x), then centre
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


# -- the beam ---------------------------------------------------------------------------

ORIGIN = np.array([0.104, 0.0, 0.0])
AXIS = np.array([-0.04, 0.01, 1.0]) / np.linalg.norm([-0.04, 0.01, 1.0])


def _beam_points(zs, outlier=None):
    pts = [ORIGIN + (z / AXIS[2]) * AXIS for z in zs]
    if outlier is not None:
        pts[outlier] = pts[outlier] + np.array([0.03, 0.0, 0.0])
    return pts


def test_the_beam_is_fitted_and_an_outlier_trimmed():
    fit = pose.fit_pose_points(_beam_points([0.6, 0.9, 1.2, 1.5, 1.8, 2.1], outlier=3))
    assert fit.outcome == "accepted"
    assert fit.used == [0, 1, 2, 4, 5]
    assert np.allclose(fit.laser_position, ORIGIN, atol=1e-6)
    assert np.allclose(fit.laser_axis, AXIS, atol=1e-6)


def test_frames_without_a_point_are_skipped_but_keep_their_index():
    pts = _beam_points([0.6, 1.0, 1.4, 1.8])
    pts.insert(1, None)
    fit = pose.fit_pose_points(pts)
    assert fit.outcome == "accepted" and fit.used == [0, 2, 3, 4]


def test_points_too_close_in_range_are_refused():
    fit = pose.fit_pose_points(_beam_points([1.0, 1.1, 1.2, 1.3]))
    assert (fit.outcome, fit.refusal_reason) == ("refused", "lever_arm_below_0.6m")


def test_fewer_than_three_points_are_refused():
    fit = pose.fit_pose_points(_beam_points([0.6, 1.8]) + [None])
    assert (fit.outcome, fit.refusal_reason) == ("refused", "too_few_frames")


def test_three_points_are_never_trimmed():
    """With 3 nothing can say which is wrong: none is dropped (owner, v3)."""
    # one far enough off that a fourth point would see it trimmed
    pts = _beam_points([0.94, 1.64, 2.02])
    pts[1] = pts[1] + np.array([-0.086, 0.066, 0.0])
    fit = pose.fit_pose_points(pts)
    assert fit.used == [0, 1, 2]


# -- a scan larger than the board (the V-Slates, 2026-10-23) -----------------------------

#: The V-Slate templates are 8.5 x 14 in scans of the physical slate, with the
#: 12 in board starting ~1.9 in from the page's left edge: centring the page on
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
