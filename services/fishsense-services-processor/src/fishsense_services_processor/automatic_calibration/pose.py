"""Calibration v4: the slate's pose found automatically, then production's
method -- the dot's ray meets the slate's plane, a beam is fitted through the
points (owner, 2026-10-23).

Why: size constancy (v2/v3) extrapolates to a vanishing point and fixes scale
with an assumed laser offset, so it is biased where divers tilt the slate more
at distance -- the field protocol told them tilt didn't matter, which is true
for production's per-frame pose and false for size constancy (reef missions
347, 349, 436). Production's slate fit solves each frame's pose from clicked
reference points; this finds them instead:

1. **Board** (`fit_board`): the rigid 8.5 x 12 in rounded board fitted through
   the camera to SAM's slate mask. Ported from 2026-10-03_slate_detector@4fa1c8d
   ``rectfit.py`` (fit, start quads, tilt prior), without its third start quad
   (``maskqueue.fit_quad``): the minimum enclosing quad and the min-area
   rectangle are kept.
2. **Pattern** (`align_template`): the board rectified front-on from its
   corners, and the template's page aligned to it by ECC (intensity
   correlation, homography), both ways up. SIFT failed on these sparse, bold,
   symmetric patterns (2026-10-23: 31% matched, all wrong); ECC put the
   reference points a median 2.0 px from the human clicks on 326 pool frames.
3. **Pose** (`slate_plane`): production's pose (`pose_from_correspondences`,
   one solve) on the template's reference points as the alignment places
   them.
4. **Point** (`dot_point`: production's `laser_point_on_plane`): the
   automatic dot's ray meeting that plane.
5. **Beam** (`fit_pose_points`): a line through the points, trimming one at a
   time the point furthest off it (relative to its range) beyond
   `TRIM_MADS` robust SDs; production's lever-arm gate (0.6 m).

On the 1600-px cache (local, 2026-10-23): pool beams within 1.7% of each
frame's human-pose range (11 missions, median 0.8%); reef 341/349/436 4-8%,
with weaker alignments -- the cache is 2.5x under full resolution, which this
uses.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Optional, Sequence

import cv2
import numpy as np
from fishsense_core.calibration_checks import (
    CalibrationUnderdeterminedError,
    check_observation_geometry,
)
from fishsense_core.plane import (
    Plane,
    laser_point_on_plane,
    plane_from_pose,
    pose_from_correspondences,
)

from fishsense_services_processor.automatic_calibration.label_free import (
    LabelFreeFit,
    accepted,
    refused,
)

#: As production's slate fit (`laser_calibration.activities`).
INCH_TO_M = 0.0254

__all__ = [
    "ASPECT",
    "BoardFit",
    "ECC_MIN",
    "align_template",
    "dot_point",
    "fit_board",
    "fit_pose_points",
    "slate_plane",
    "tilt_deg",
]

# -- 1. the board (ported from rectfit.py) ---------------------------------------------

#: Every slate is the same 8.5 x 12 in board with rounded corners (aspect and
#: radius measured on barely occluded frames of all families, 2026-10-06).
ASPECT = 0.708
RADIUS = 0.031
MISS = 1.0
EXTRA = 0.15
#: Divers mostly face the slate to the camera: a steep tilt needs evidence.
TILT_PRIOR = 0.04


def _outline(aspect: float, radius: float, n_arc: int = 6) -> np.ndarray:
    r = float(np.clip(radius, 0.0, 0.49 * min(1.0, aspect)))
    if r <= 1e-6:
        pts = [(0, 0), (1, 0), (1, aspect), (0, aspect)]
    else:
        pts = []
        for (cx, cy), a0 in (((r, r), np.pi), ((1 - r, r), 1.5 * np.pi),
                             ((1 - r, aspect - r), 0.0), ((r, aspect - r), 0.5 * np.pi)):  # fmt: skip
            for t in np.linspace(a0, a0 + 0.5 * np.pi, n_arc):
                pts.append((cx + r * np.cos(t), cy + r * np.sin(t)))
    out = np.zeros((len(pts), 3))
    out[:, :2] = pts
    return out


def _project(points3d, rvec, tvec, k) -> np.ndarray:
    img, _ = cv2.projectPoints(
        points3d, np.asarray(rvec, float), np.asarray(tvec, float), k, None
    )
    return img.reshape(-1, 2)


def _clockwise(quad) -> np.ndarray:
    q = np.asarray(quad, float)
    c = q.mean(0)
    q = q[np.argsort(np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0]))]
    return np.roll(q, -int(np.argmin(q[:, 0] + q[:, 1])), axis=0)


def _pose_from_quad(quad, aspect, k, rotation):
    q = _clockwise(quad)
    obj = _outline(aspect, 0.0)
    img = np.roll(q, -rotation, axis=0)
    ok, rv, tv = cv2.solvePnP(obj, img, k, None, flags=cv2.SOLVEPNP_IPPE)
    if not ok or tv[2] <= 0:
        raise ValueError("no pose places the board in front of the camera")
    return rv.ravel(), tv.ravel()


def _line(p, q):
    a, b = q[1] - p[1], p[0] - q[0]
    return a, b, a * p[0] + b * p[1]


def _meet(l1, l2):
    (a1, b1, c1), (a2, b2, c2) = l1, l2
    det = a1 * b2 - a2 * b1
    return None if abs(det) < 1e-9 else np.array([(c1 * b2 - c2 * b1) / det,
                                                   (a1 * c2 - a2 * c1) / det])  # fmt: skip


def _min_enclosing_quad(mask, eps=1.5, max_edges=14):
    cs, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if not cs:
        return None
    hull = cv2.convexHull(max(cs, key=cv2.contourArea)).astype(np.float32)
    poly = cv2.approxPolyDP(hull, eps, True).reshape(-1, 2).astype(float)
    n = len(poly)
    if n < 4:
        return None
    edges = [_line(poly[i], poly[(i + 1) % n]) for i in range(n)]
    lengths = [np.hypot(*(poly[(i + 1) % n] - poly[i])) for i in range(n)]
    longest = sorted(np.argsort(lengths)[::-1][:max_edges])
    best, best_area = None, np.inf
    for combo in itertools.combinations(longest, 4):
        corners = [_meet(edges[combo[k - 1]], edges[combo[k]]) for k in range(4)]
        if any(c is None for c in corners):
            continue
        q = np.array(corners)
        area = 0.5 * abs(
            np.dot(q[:, 0], np.roll(q[:, 1], -1))
            - np.dot(q[:, 1], np.roll(q[:, 0], -1))
        )
        if not 0 < area < best_area:
            continue
        qc = q.astype(np.float32).reshape(-1, 1, 2)
        if cv2.contourArea(cv2.convexHull(qc)) < 0.999 * area:
            continue
        if all(
            cv2.pointPolygonTest(qc, (float(x), float(y)), True) >= -1.0
            for x, y in poly
        ):
            best, best_area = q, area
    return best


def _raster(poly, shape, offset):
    m = np.zeros(shape, np.uint8)
    cv2.fillPoly(m, [np.int32(np.round((poly - offset) * 4))], 1, shift=2)
    return m.astype(bool)


def tilt_deg(rvec) -> float:
    """The board's normal against the camera axis; 0 = facing the camera."""
    r, _ = cv2.Rodrigues(np.asarray(rvec, float))
    return float(np.degrees(np.arccos(min(1.0, abs(r[2, 2])))))


@dataclass(frozen=True)
class BoardFit:
    corners: np.ndarray  # TL, TR, BR, BL, image px
    rvec: np.ndarray
    tvec: np.ndarray
    cost: float
    covered: float
    extra: float

    @property
    def plausible(self) -> bool:
        return self.covered >= 0.97 and self.extra <= 0.35


def _fit(mask, k, init_quad, rotation, pad=0.35) -> BoardFit:
    from scipy.optimize import minimize  # pylint: disable=import-outside-toplevel

    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    w, h = x1 - x0, y1 - y0
    X0, Y0 = max(0, int(x0 - pad * w)), max(0, int(y0 - pad * h))
    X1, Y1 = min(mask.shape[1], int(x1 + pad * w)), min(
        mask.shape[0], int(y1 + pad * h)
    )
    crop = mask[Y0:Y1, X0:X1]
    offset = np.array([X0, Y0], float)
    n_mask = max(int(crop.sum()), 1)
    outline = _outline(ASPECT, RADIUS)
    rv0, tv0 = _pose_from_quad(init_quad, ASPECT, k, rotation)

    def cost(x):
        rv, tv = x[:3], x[3:]
        if tv[2] <= 1e-3:
            return 1e9
        poly = _project(outline, rv, tv, k)
        r = _raster(poly, crop.shape, offset)
        miss = np.count_nonzero(crop & ~r)
        extra = np.count_nonzero(r & ~crop)
        area = 0.5 * abs(np.dot(poly[:, 0], np.roll(poly[:, 1], -1))
                         - np.dot(poly[:, 1], np.roll(poly[:, 0], -1)))  # fmt: skip
        extra += max(0.0, area - r.sum())
        return (MISS * miss + EXTRA * extra) / n_mask

    res = minimize(cost, np.concatenate([rv0, tv0]), method="Powell",
                   options={"xtol": 1e-4, "ftol": 1e-5, "maxfev": 4000})  # fmt: skip
    rv, tv = res.x[:3], res.x[3:]
    r = _raster(_project(outline, rv, tv, k), crop.shape, offset)
    return BoardFit(corners=_project(_outline(ASPECT, 0.0), rv, tv, k), rvec=rv, tvec=tv,
                    cost=float(res.fun), covered=float((crop & r).sum() / n_mask),
                    extra=float((r & ~crop).sum() / max(r.sum(), 1)))  # fmt: skip


def fit_board(mask: np.ndarray, camera_matrix) -> Optional[BoardFit]:
    """The board's pose that best explains SAM's `mask` (in the camera
    `camera_matrix` describes, at the mask's resolution); None if none does."""
    k = np.asarray(camera_matrix, float)
    starts = []
    q = _min_enclosing_quad(mask)
    if q is not None:
        starts.append(q)
    cs, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if cs:
        starts.append(
            cv2.boxPoints(cv2.minAreaRect(max(cs, key=cv2.contourArea))).astype(float)
        )
    fits = []
    for start in starts:
        for rotation in range(4):
            try:
                fits.append(_fit(mask, k, start, rotation))
            except ValueError:
                continue
    if not fits:
        return None
    return min(
        fits,
        key=lambda f: f.cost
        + TILT_PRIOR * (1.0 - np.cos(np.radians(tilt_deg(f.rvec)))),
    )


# -- 2. the printed pattern -------------------------------------------------------------

#: The alignment's correlation below which a frame's pose is not used: pool
#: frames aligned at 0.8-1.0 put the points a median 1.9-2.1 px from the
#: human clicks (2026-10-23). To be checked on full resolution.
ECC_MIN = 0.8
#: The board rectified to this long side for the alignment (as the 1600-px
#: cache prototype, 2026-10-23).
BOARD_LONG_PX = 1000
_BOARD_LONG_IN = 12.0


#: How far the coarse search looks for the page on the board, beyond any
#: overhang of a page larger than the board.
SEARCH_IN = 3.0


def _page_at(board_blur: np.ndarray, page_blur: np.ndarray, reach: int):
    """``(dx, dy, score)``: where the page's top-left sits on the board (board
    px, may be negative) by normalised cross-correlation, the board padded by
    `reach` px of its mean so the page may overhang it."""
    pad = cv2.copyMakeBorder(board_blur, reach, reach, reach, reach,
                             cv2.BORDER_CONSTANT, value=float(board_blur.mean()))  # fmt: skip
    if page_blur.shape[0] > pad.shape[0] or page_blur.shape[1] > pad.shape[1]:
        return None
    scores = cv2.matchTemplate(pad, page_blur, cv2.TM_CCOEFF_NORMED)
    _, best, _, (x, y) = cv2.minMaxLoc(scores)
    return x - reach, y - reach, float(best)


@dataclass(frozen=True)
class Alignment:
    """The page aligned to the board: `h` maps template pixels (the page at
    its dpi) to image pixels, `ecc` its correlation; `ecc_other` the other
    way up's (None if it did not converge), `page_up_deg` the direction of the
    page's top in the image (0 = up, clockwise positive)."""

    h: np.ndarray
    ecc: float
    ecc_other: Optional[float]
    page_up_deg: float


def align_template(image_grey: np.ndarray, corners, page_grey: np.ndarray, dpi: int):
    """``(H, correlation)`` of `align_template_both`, or ``(None, None)``."""
    a = align_template_both(image_grey, corners, page_grey, dpi)
    return (None, None) if a is None else (a.h, a.ecc)


def align_template_both(
    image_grey: np.ndarray, corners, page_grey: np.ndarray, dpi: int
) -> Optional[Alignment]:
    """The page aligned to the board both ways up, the better kept: an
    `Alignment`, or None. `corners` are the board's TL, TR, BR, BL in the
    image.

    Both orientations' correlations are kept (2026-10-25): on a
    near-symmetric pattern v4 chose the wrong way up on 17 of 445
    human-labelled frames (436's 11 of 12), and nothing else it recorded
    told them apart; the losing one's correlation is the margin a check
    needs.

    The board is rectified front-on at its known size (12 in long), so the
    page's scale on it is known from `dpi`; where the page sits is not. The
    templates are scans of the physical slates, some larger than the board
    (the V-Slates: 14 in, the board ~1.9 in from the edge), so the page is
    first found on the board by cross-correlation (`_page_at`, within
    `SEARCH_IN` beyond any overhang), both ways up, then refined by ECC.
    (Until 2026-10-23 it was centred on the board, ~1 in off for a V-Slate:
    mission 436's poses settled the wrong way round.)"""
    corners = np.asarray(corners, np.float32)
    s01 = np.linalg.norm(corners[1] - corners[0])
    s12 = np.linalg.norm(corners[2] - corners[1])
    short = int(BOARD_LONG_PX * ASPECT)
    w, h = (BOARD_LONG_PX, short) if s01 >= s12 else (short, BOARD_LONG_PX)
    hb = cv2.getPerspectiveTransform(
        corners, np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    )
    board = cv2.warpPerspective(image_grey, hb, (w, h))
    ph, pw = page_grey.shape
    rotate = (pw >= ph) != (w >= h)
    page = cv2.rotate(page_grey, cv2.ROTATE_90_CLOCKWISE) if rotate else page_grey
    rh, rw = page.shape
    px_per_in = max(w, h) / _BOARD_LONG_IN
    f = px_per_in / dpi
    sw, sh = int(round(rw * f)), int(round(rh * f))
    scaled = cv2.resize(page, (sw, sh), interpolation=cv2.INTER_AREA)
    over = max(sw - w, sh - h, 0)
    reach = int(over + SEARCH_IN * px_per_in)
    a = cv2.GaussianBlur(cv2.equalizeHist(board), (0, 0), 3).astype(np.float32)
    scored = []
    for flip in (False, True):
        fl = (np.array([[-1, 0, sw], [0, -1, sh], [0, 0, 1]], np.float64) if flip
              else np.eye(3))  # fmt: skip
        shown = cv2.rotate(scaled, cv2.ROTATE_180) if flip else scaled
        at = _page_at(a, cv2.GaussianBlur(shown, (0, 0), 3).astype(np.float32), reach)
        if at is None:
            continue
        dx, dy, _ = at
        hinit = (np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]], np.float64) @ fl
                 @ np.diag([f, f, 1.0])).astype(np.float32)  # fmt: skip
        on_board = cv2.warpPerspective(page, hinit, (w, h), flags=cv2.INTER_AREA,
                                       borderValue=255)  # fmt: skip
        b = cv2.GaussianBlur(on_board, (0, 0), 3).astype(np.float32)
        try:
            cc, warp = cv2.findTransformECC(
                b, a, np.eye(3, dtype=np.float32), cv2.MOTION_HOMOGRAPHY,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6), None, 5)  # fmt: skip
        except cv2.error:
            continue
        scored.append((float(cc), warp @ hinit))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    cc, ht = scored[0]
    if rotate:  # unrotated page px -> rotated: (x, y) -> (ph - 1 - y, x)
        ht = ht @ np.array([[0, -1, ph - 1], [1, 0, 0], [0, 0, 1]], np.float32)
    h = np.linalg.inv(hb) @ ht
    gh, gw = page_grey.shape
    top, bottom = cv2.perspectiveTransform(
        np.float32([[[gw / 2, 0]], [[gw / 2, gh]]]), h.astype(np.float32)
    ).reshape(2, 2)
    dx, dy = top - bottom
    return Alignment(
        h=h, ecc=cc, ecc_other=scored[1][0] if len(scored) > 1 else None,
        page_up_deg=float(np.degrees(np.arctan2(dx, -dy))),
    )  # fmt: skip


# -- 3. the slate's plane ---------------------------------------------------------------


def slate_plane(reference_points, dpi: int, h_template_to_image, camera_matrix):
    """``(plane, image points, rms px)``, or None: the template's reference
    points as the alignment places them, through production's
    `pose_from_correspondences` (template px / dpi, in metres). The rms is
    how far a rigid board's pose misses those points: the alignment is a free
    homography, so this is how much of it no real pose explains."""
    tpl = np.asarray(reference_points, np.float32).reshape(-1, 1, 2)
    h = np.asarray(h_template_to_image, float)
    img = cv2.perspectiveTransform(tpl, h).reshape(-1, 2)
    body = np.zeros((len(img), 3))
    body[:, :2] = np.asarray(reference_points, float) / float(dpi) * INCH_TO_M
    k = np.asarray(camera_matrix, float)
    # One solve for the plane and its rms (review of #350): a second solvePnP
    # here cost twice and, given float64 where core casts to float32, could
    # describe a slightly different pose from the plane returned.
    solved = pose_from_correspondences(body, img, k)
    if solved is None:
        return None
    rv, tv = solved
    plane = plane_from_pose(cv2.Rodrigues(rv)[0], tv)
    rms = float(np.sqrt(((_project(body, rv, tv, k) - img) ** 2).sum(1).mean()))
    return plane, img, rms


def dot_point(plane: Plane, dot, camera_matrix) -> Optional[np.ndarray]:
    """The dot's ray meeting the plane (production's `laser_point_on_plane`);
    None behind the camera."""
    p = laser_point_on_plane(plane, dot, np.asarray(camera_matrix, float))
    return None if p is None or not np.all(np.isfinite(p)) or p[2] <= 0 else p


# -- 5. the beam ------------------------------------------------------------------------

#: A point is trimmed while it is this many robust SDs off the line (distance
#: relative to its range: a pose error scales with range), with a 1% floor.
TRIM_MADS = 3.0
_TRIM_FLOOR = 0.01


def _line_through(points: np.ndarray):
    c = points.mean(0)
    d = np.linalg.svd(points - c)[2][0]
    return c, (d if d[2] > 0 else -d)


def fit_pose_points(points, *, min_frames: int = 3) -> LabelFreeFit:
    """The beam through the slate points (camera space, metres): trimmed one
    at a time, the lever-arm gate, the origin where the beam crosses the
    camera's plane (z = 0), as the label-free fits report it."""
    pts = np.asarray([p for p in points if p is not None], float).reshape(-1, 3)
    idx = [i for i, p in enumerate(points) if p is not None]
    keep = np.arange(len(pts))
    seen = dict(used=[idx[i] for i in keep], frames_used=len(keep), pairs=0,
                pair_residual_sd=None)  # fmt: skip
    if len(pts) < min_frames:
        return refused("too_few_frames", **seen)
    while len(keep) > max(min_frames, 3):
        c, d = _line_through(pts[keep])
        r = np.linalg.norm(np.cross(pts[keep] - c, d), axis=1) / pts[keep][:, 2]
        mad = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-4
        worst = int(np.argmax(r))
        if r[worst] <= max(np.median(r) + TRIM_MADS * mad, _TRIM_FLOOR):
            break
        keep = np.delete(keep, worst)
    seen.update(used=[idx[i] for i in keep], frames_used=len(keep))
    try:
        check_observation_geometry(pts[keep])
    except CalibrationUnderdeterminedError:
        return refused("lever_arm_below_0.6m", **seen)
    c, d = _line_through(pts[keep])
    origin = c - (c[2] / d[2]) * d
    return accepted(
        laser_position=[float(v) for v in origin], laser_axis=[float(v) for v in d],
        o_mag_m=float(np.linalg.norm(origin)), **seen,
    )  # fmt: skip
