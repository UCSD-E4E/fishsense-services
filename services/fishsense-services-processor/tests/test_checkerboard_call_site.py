"""Core's checkerboard detector at services' call site.

The detector is fishsense-core's (`fishsense_core.checkerboard`, 4.3.1: #98, #114)
and core's suite tests it alone. These are services' own tests of it, kept
from services' `tests/test_checkerboard_detection.py` (which tested services'
copy, `checkerboard/detection.py`, through v0.56.0) because they pin the
combination the checkerboard activities actually run, which core's suite does
not (review of #353):

  * the detector's correspondences through `fishsense_core.plane`: a sub-grid,
    a partial view and a transposed or mirrored grid recover the same plane;
  * its `board_hull` through the contracts' `point_in_laser_region`, the
    region test both activities admit a dot by: the hull is the grid's
    outline in order, inset one square, and an occluder refuses a dot on it.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from fishsense_core import checkerboard as sut
from fishsense_core.plane import (
    laser_point_on_plane,
    plane_from_correspondences,
)

from fishsense_services_contracts.laser_region import point_in_laser_region

SQUARE_PX = 40
SQUARE_SIZE_M = 0.0254

# The E4E board: 15 x 11 squares -> 14 x 10 interior corners.
BOARD_COLS_SQ, BOARD_ROWS_SQ = 15, 11
BOARD_COLS, BOARD_ROWS = BOARD_COLS_SQ - 1, BOARD_ROWS_SQ - 1

CAMERA_MATRIX = np.array([[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]])


def _render(cols_sq: int = BOARD_COLS_SQ, rows_sq: int = BOARD_ROWS_SQ, border=80):
    """A flat checkerboard on a white ground.

    The white border is not decoration — `findChessboardCornersSB` requires a
    quiet boundary around the pattern.
    """
    height = rows_sq * SQUARE_PX + 2 * border
    width = cols_sq * SQUARE_PX + 2 * border
    img = np.full((height, width), 255, np.uint8)
    for row in range(rows_sq):
        for col in range(cols_sq):
            if (row + col) % 2 == 0:
                y_0, x_0 = border + row * SQUARE_PX, border + col * SQUARE_PX
                img[y_0 : y_0 + SQUARE_PX, x_0 : x_0 + SQUARE_PX] = 0
    return img


def _warped(img):
    """The board seen at an angle, so the recovered pose is a real one."""
    height, width = img.shape[:2]
    source = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    destination = np.float32(
        [
            [width * 0.10, height * 0.05],
            [width * 0.92, height * 0.16],
            [width * 0.88, height * 0.95],
            [width * 0.06, height * 0.86],
        ]
    )
    matrix = cv2.getPerspectiveTransform(source, destination)
    return cv2.warpPerspective(img, matrix, (width, height), borderValue=255)


def _detect(img, **overrides):
    kwargs = {
        "max_rows": BOARD_ROWS,
        "max_cols": BOARD_COLS,
        "pitch_x_m": SQUARE_SIZE_M,
        "pitch_y_m": SQUARE_SIZE_M,
    }
    kwargs.update(overrides)
    return sut.detect_checkerboard(img, **kwargs)


# ---------- the happy path ----------


def test_correspondences_recover_a_plane():
    detected = _detect(_warped(_render()))
    assert detected is not None

    plane = plane_from_correspondences(
        detected.body_points, detected.image_points, CAMERA_MATRIX
    )

    assert plane is not None
    assert np.isclose(np.linalg.norm(plane.normal), 1.0)


# ---------- partial views: the property that makes them usable ----------


def test_a_partial_view_is_detected_as_a_sub_grid():
    """Occlusion and the frame edge cut the board down; ~96% still detect.

    Note the exact patternSize would fail outright here — which is why the
    detector asks for a small grid and reads the real one back from the
    detector's own metadata, rather than asserting the nominal board.
    """
    board = _warped(_render())
    cropped = board[:, : int(board.shape[1] * 0.66)].copy()

    detected = _detect(cropped)

    assert detected is not None
    assert detected.rows * detected.cols < BOARD_ROWS * BOARD_COLS


def _project(body_points, rvec, tvec):
    """Where a body grid lands in a rectified image, exactly.

    Analytic rather than detected, because the property below is about the
    geometry and detected corners carry sub-pixel noise that would swamp it —
    the noise is characterised separately in the next test.
    """
    points_3d = np.column_stack(
        [body_points, np.zeros(len(body_points), dtype=np.float64)]
    )
    projected, _ = cv2.projectPoints(
        points_3d, rvec, tvec, CAMERA_MATRIX, np.zeros((5,))
    )
    return projected.reshape(-1, 2)


def test_a_sub_grid_recovers_the_identical_plane():
    """The whole justification for accepting sub-grids.

    A sub-grid of a regular grid has the same pitch, and only the plane
    reaches the answer — the sub-board's unknown offset into the full board
    lands in the in-plane pose, which nothing downstream reads. So a detector
    that returns 10 x 10 of a 14 x 10 board, with no idea *which* 10 x 10, is
    still answering the question stage 13 asks.
    """
    rvec = np.array([0.28, -0.16, 0.07])
    tvec = np.array([-0.10, -0.08, 1.40])
    grid = np.stack(
        np.meshgrid(np.arange(BOARD_COLS), np.arange(BOARD_ROWS)), axis=-1
    ).astype(np.float64)
    body = grid * SQUARE_SIZE_M
    image = _project(body.reshape(-1, 2), rvec, tvec).reshape(BOARD_ROWS, BOARD_COLS, 2)

    dot = np.array([620.0, 500.0])

    def _hit(body_grid, image_grid):
        points = body_grid.reshape(-1, 2)
        # Re-based to the sub-grid's own origin, as the detector emits it.
        points = points - points.min(axis=0)
        plane = plane_from_correspondences(
            points, image_grid.reshape(-1, 2), CAMERA_MATRIX
        )
        return laser_point_on_plane(plane, dot, CAMERA_MATRIX)

    whole = _hit(body, image)
    for rows, cols in (
        (slice(2, None), slice(None, 7)),
        (slice(None, 5), slice(4, None)),
    ):
        assert np.allclose(whole, _hit(body[rows, cols], image[rows, cols]), atol=1e-6)


def test_a_partial_view_agrees_with_the_full_board_to_within_its_conditioning():
    """Detected independently, a sub-grid answers the same question less sharply.

    Not a contradiction of the exact property above: the corners themselves
    agree to 0.025 px between the two runs, but a 10 x 10 sub-grid spans less
    of the frame than the full 14 x 10, so the same corner error buys a worse
    pose. Measured here at ~1.6% of a 1.43 m depth.

    Recorded rather than gated on, because the defence against it is already
    in place and is the right one: `calibrate_laser` is a least-squares fit
    over every observation in the dive (28-133 frames in this corpus, against
    a `MIN_LASER_POINTS` of 2), and `check_fit_self_consistency` refuses a fit
    that disagrees with the dots it came from. Discarding small detections
    instead would throw away usable frames to fix a term the aggregate already
    handles.
    """
    board = _warped(_render())
    cropped = board[:, : int(board.shape[1] * 0.66)].copy()

    full = _detect(board)
    partial = _detect(cropped)
    assert full is not None and partial is not None
    assert (partial.rows, partial.cols) != (full.rows, full.cols)

    dot = np.array([620.0, 500.0])
    from_full = laser_point_on_plane(
        plane_from_correspondences(full.body_points, full.image_points, CAMERA_MATRIX),
        dot,
        CAMERA_MATRIX,
    )
    from_partial = laser_point_on_plane(
        plane_from_correspondences(
            partial.body_points, partial.image_points, CAMERA_MATRIX
        ),
        dot,
        CAMERA_MATRIX,
    )

    relative = np.linalg.norm(from_full - from_partial) / np.linalg.norm(from_full)
    assert relative < 0.03, relative


def test_the_grid_orientation_does_not_change_the_plane():
    """The detected grid may come back transposed or mirrored, and must not matter.

    The board is a symmetric grid, so the detector's indexing is only defined
    up to the 8 symmetries of a rectangle. A mirrored assignment is realised
    by a real rotation (the board flipped 180 degrees about an in-plane axis),
    which flips the plane normal — and the ray-plane intersection divides one
    normal by the other, so the sign cancels.

    This is why the classic checkerboard headaches (180-degree ambiguity,
    which corner is the origin, consistent ordering between frames) are all
    irrelevant to *this* use, and why building body points on the detected
    shape is safe.
    """
    detected = _detect(_warped(_render()))
    assert detected is not None

    body = detected.body_points.reshape(detected.rows, detected.cols, 2)
    image = detected.image_points.reshape(detected.rows, detected.cols, 2)

    dot = np.array([620.0, 500.0])
    straight = laser_point_on_plane(
        plane_from_correspondences(
            body.reshape(-1, 2), image.reshape(-1, 2), CAMERA_MATRIX
        ),
        dot,
        CAMERA_MATRIX,
    )
    # Mirror the image traversal while leaving the body grid as it is: the
    # body frame is now a reflection of the physical board's.
    mirrored = laser_point_on_plane(
        plane_from_correspondences(
            body.reshape(-1, 2), image[:, ::-1, :].reshape(-1, 2), CAMERA_MATRIX
        ),
        dot,
        CAMERA_MATRIX,
    )

    assert np.allclose(straight, mirrored, atol=1e-3)


# ---------- is the dot actually ON the board? ----------
#
# The calibration assumes the dot lies on the board's plane, and
# `laser_point_on_plane` intersects the camera ray with that plane whether or
# not the physical board reaches that far — a dot that missed and landed on the
# floor behind gets a confident, wrong depth. Nothing downstream can see it:
# `check_fit_self_consistency` compares the fitted ray's reprojection against
# the 2-D dot line, and that line is the laser's epipolar line, identical
# either way. The dots are right; only the depths are wrong.
#
# So the frame has to answer it, and the detected corners already do. This is
# the checkerboard's equivalent of the `Slate, Laser on slate` marker, minus
# the human.


def _accepts(img, point) -> bool:
    """Would this frame contribute an observation with the dot at `point`?

    False covers both refusals — no trustworthy board detected, or a board
    whose hull does not contain the dot. They are the same outcome for the
    caller: the frame is dropped rather than fitted.
    """
    detected = _detect(img)
    if detected is None:
        return False
    return point_in_laser_region(point[0], point[1], sut.board_hull(detected))


def test_board_hull_is_the_grid_outline_in_order():
    """Four vertices, traversing the quad — `point_in_laser_region` is a
    convex same-side test, so a scrambled order would admit the wrong set."""
    detected = _detect(_warped(_render()))
    hull = sut.board_hull(detected)

    assert len(hull) == 4
    grid = detected.image_points.reshape(detected.rows, detected.cols, 2)
    for expected, actual in zip(
        [grid[0, 0], grid[0, -1], grid[-1, -1], grid[-1, 0]], hull, strict=True
    ):
        assert np.allclose(expected, actual)


def test_a_dot_on_the_board_is_accepted():
    board = _warped(_render())
    height, width = board.shape[:2]

    assert _accepts(board, (width // 2, height // 2))


def test_a_dot_off_the_board_is_rejected():
    """The case the gate exists for: the laser missed and hit what was behind."""
    board = _warped(_render())

    assert not _accepts(board, (5.0, 5.0))


def test_the_hull_is_inset_by_one_square_and_that_is_deliberate():
    """Interior corners only, so the hull sits one full square inside the board.

    A dot in that outer ring is genuinely on the board and is still refused.
    That is the conservative direction and it is chosen: dilating the hull
    outward to reach the physical edge would also erode the occlusion margin
    the gate depends on by exactly one square, which is what makes a
    two-square occluder detectable. There are 28-113 dotted frames per dive
    against a `MIN_LASER_POINTS` of 2, so frames are the cheap thing here.
    """
    detected = _detect(_render())
    grid = detected.image_points.reshape(detected.rows, detected.cols, 2)
    # One square outward from a corner, along the grid's own diagonal.
    outer = grid[0, 0] + (grid[0, 0] - grid[1, 1]) * 0.5

    assert not point_in_laser_region(
        float(outer[0]), float(outer[1]), sut.board_hull(detected)
    )


@pytest.mark.parametrize("squares", [2, 3, 4, 6])
def test_an_occluder_means_a_dot_on_it_is_refused(squares):
    """The coupling the gate leans on, measured rather than assumed.

    Something occluding the board removes the corners under it, so the
    detector returns a sub-rectangle beside it and the hull no longer covers
    the occluder. Measured against OpenCV 4.13: a 2x2-square occluder collapses
    a 10x14 grid to 10x6, a 4x4 to 5x10, and half the board occluded still
    detects 7x10 and still refuses a dot in the occluded half.

    The boundary is roughly one square: an occluder that small leaves the grid
    intact and a dot on it passes. That needs a sub-4cm object sitting exactly
    where the dot lands, and an off-board point sits off the common laser ray,
    so the fit's own residual is the second line.
    """
    board = _warped(_render())
    height, width = board.shape[:2]
    size = squares * SQUARE_PX
    centre_y, centre_x = height // 2, width // 2
    occluded = board.copy()
    occluded[
        centre_y - size // 2 : centre_y + size // 2,
        centre_x - size // 2 : centre_x + size // 2,
    ] = 128

    assert not _accepts(occluded, (centre_x, centre_y))
