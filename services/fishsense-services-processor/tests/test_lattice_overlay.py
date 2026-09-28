"""Pure-logic tests for the checkerboard lattice verification overlay.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_lattice_overlay.py.

This overlay exists to answer one question a human can settle in a second and
no residual can settle at all: **are the detected corners one board square
apart, or two?**

`grid_residual_px` cannot tell, and that is not a gap in the threshold — it is
structural. A uniformly mis-latticed grid (every corner two squares apart,
`body_points` labelling them one apart) is still a perfect grid, so it is an
exact homography of the modelled grid and the residual is ~0. The guard in
`checkerboard_detection` catches a grid whose spacing is *mixed*, which breaks
regularity; the uniform case sails through it, through `_fits_declared_board`
(a coarse lattice is smaller than the declared board, so it passes an upper
bound), and through `check_fit_self_consistency` (which compares 2-D dots that
a pure scale error does not move). It then scales every depth, the fitted
baseline, and every length downstream.

Measured 2026-09-11: five of fourteen checkerboard calibrations carry a
baseline that is a simple multiple of the consensus 10.4 cm -- 4.00x, 2.16x,
2.12x, 1.51x and 0.47x -- which is the signature of exactly this. This overlay
puts the lattice in front of a labeler so that inference can be confirmed or
killed.

**The connecting lines are the point, not decoration.** Isolated corner dots
turn "is this every corner or every other one?" into a counting exercise at
high zoom. Lines make a wrong lattice self-evident: its cells visibly span two
board squares.
"""

import cv2
import numpy as np

from fishsense_services_processor.checkerboard.lattice_overlay import (
    draw_lattice_overlay,
)


def _make_image(height: int = 1200, width: int = 1600) -> np.ndarray:
    return np.full((height, width, 3), fill_value=128, dtype=np.uint8)


def _grid_points(rows: int, cols: int, *, spacing: float, origin=(200.0, 150.0)):
    """Row-major corners, the same order `detect_checkerboard` returns."""
    x0, y0 = origin
    return np.array(
        [
            [x0 + c * spacing, y0 + r * spacing]
            for r in range(rows)
            for c in range(cols)
        ],
        dtype=np.float64,
    )


def test_returns_a_new_array_and_does_not_mutate_input():
    img = _make_image()
    before = img.copy()
    out = draw_lattice_overlay(
        img, rows=4, cols=5, image_points=_grid_points(4, 5, spacing=60)
    )
    assert out is not img
    assert np.array_equal(img, before)


def test_keeps_input_shape_and_dtype():
    img = _make_image(900, 1300)
    out = draw_lattice_overlay(
        img, rows=3, cols=4, image_points=_grid_points(3, 4, spacing=50)
    )
    assert out.shape == img.shape
    assert out.dtype == img.dtype


def test_marks_every_detected_corner():
    """One mark per corner — a labeler counting marks is counting corners."""
    points = _grid_points(4, 5, spacing=60)
    out = draw_lattice_overlay(_make_image(), rows=4, cols=5, image_points=points)
    for x, y in points:
        patch = out[int(y) - 2 : int(y) + 3, int(x) - 2 : int(x) + 3]
        assert not np.all(patch == 128), f"no mark drawn at ({x}, {y})"


def test_draws_lines_between_row_adjacent_corners():
    """The midpoint of each horizontal edge is painted.

    This is what distinguishes a correct lattice from a coarse one by eye: the
    drawn cell either matches a board square or spans two of them.
    """
    points = _grid_points(3, 4, spacing=80)
    out = draw_lattice_overlay(_make_image(), rows=3, cols=4, image_points=points)
    # Midpoint between corner (0,0) and (0,1): 40 px along x from the origin.
    mid = out[150, 240]
    assert not np.all(mid == 128)


def test_draws_lines_between_column_adjacent_corners():
    points = _grid_points(3, 4, spacing=80)
    out = draw_lattice_overlay(_make_image(), rows=3, cols=4, image_points=points)
    # Midpoint between corner (0,0) and (1,0): 40 px along y from the origin.
    mid = out[190, 200]
    assert not np.all(mid == 128)


def test_does_not_connect_the_wrap_around_between_rows():
    """The last corner of one row must not join the first of the next.

    Row-major order makes that the easy bug, and the false diagonal it draws
    reads as a lattice fault to a labeler — the overlay would manufacture the
    very finding it exists to test for.
    """
    points = _grid_points(2, 3, spacing=100, origin=(200.0, 200.0))
    out = draw_lattice_overlay(_make_image(), rows=2, cols=3, image_points=points)
    # The wrap-around would run (400, 200) -> (200, 300). Sample a quarter of
    # the way along it, at (350, 225).
    #
    # NOT its midpoint (300, 250): the legitimate column edge (300, 200) ->
    # (300, 300) passes exactly through that pixel, so the midpoint is painted
    # whether or not the bug is present and could never detect it. (350, 225)
    # lies on no legitimate edge — rows are at y=200/300, columns at
    # x=200/300/400.
    assert np.all(out[225, 350] == 128)


def test_a_coarse_lattice_is_visibly_different_from_a_fine_one():
    """The overlay must actually separate the two cases it exists to separate.

    Same board region, half as many corners: the drawn ink differs
    substantially. Without this the test suite would pass on an overlay that
    drew the same picture either way.
    """
    fine = draw_lattice_overlay(
        _make_image(), rows=5, cols=5, image_points=_grid_points(5, 5, spacing=50)
    )
    coarse = draw_lattice_overlay(
        _make_image(), rows=3, cols=3, image_points=_grid_points(3, 3, spacing=100)
    )
    fine_ink = int(np.count_nonzero(np.any(fine != 128, axis=2)))
    coarse_ink = int(np.count_nonzero(np.any(coarse != 128, axis=2)))
    assert fine_ink > coarse_ink * 1.2


def test_caption_is_drawn_when_supplied():
    """The detected grid shape travels with the frame.

    A labeler's verdict is only actionable if we know what shape was detected,
    and the LS task carries the dive elsewhere. Burning it in keeps the two
    together in the one artefact a human looks at.
    """
    points = _grid_points(3, 4, spacing=60)
    plain = draw_lattice_overlay(_make_image(), rows=3, cols=4, image_points=points)
    captioned = draw_lattice_overlay(
        _make_image(),
        rows=3,
        cols=4,
        image_points=points,
        caption="3x4  spacing 60.0px",
    )
    assert not np.array_equal(plain, captioned)


def test_survives_corners_outside_the_frame():
    """A detection may sit partly off a rectified frame's edge.

    cv2 clips, but the caller must not crash or silently drop the whole
    overlay, or the labeler sees a bare frame and reads it as "no detection".
    """
    points = _grid_points(3, 3, spacing=60, origin=(-40.0, -30.0))
    out = draw_lattice_overlay(_make_image(), rows=3, cols=3, image_points=points)
    assert out.shape == (1200, 1600, 3)
    assert np.any(out != 128)


def test_encodes_to_a_valid_jpeg_after_overlay():
    """Sanity on the seam with `encode_rectified_jpeg` downstream."""
    points = _grid_points(4, 4, spacing=70)
    out = draw_lattice_overlay(_make_image(), rows=4, cols=4, image_points=points)
    ok, encoded = cv2.imencode(".jpg", out)
    assert ok
    assert encoded.tobytes()[:2] == b"\xff\xd8"
