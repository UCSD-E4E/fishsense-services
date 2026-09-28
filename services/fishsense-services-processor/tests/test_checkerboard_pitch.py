"""v2: a calibration target's pitch is per axis, and each axis gets its own.

v1 modelled a board with one `square_size_m`. PLAN.md §4.3 asks for per-axis
pitch, because the E4E board is ~0.3-0.7 % anisotropic (wuwnet, §2.7) and one
scalar hides it; `calibration_targets` stores `pitch_x_m` and `pitch_y_m`.
Pitch is scale, and scale is the one error the rest of the pipeline provably
cannot see (`checkerboard.detection`'s module docstring), so the two must
land on the right axes.

The convention (the target's, as seeded in the API's `test_reference_data`:
the E4E board is `interior_rows=10, interior_cols=14, pitch_x_m=0.04223,
pitch_y_m=0.04211`): `pitch_x_m` is the spacing between adjacent corners
**along a row** (column to column, across the `interior_cols` axis) and
`pitch_y_m` the spacing **down a column** (row to row).

The detector's grid orientation is free (v1: the same board comes back as
10 x 14 or 14 x 10), so which detected axis is which declared axis has to be
read from the detected shape. When the shape cannot say -- a sub-grid that
fits the board either way round -- and the two pitches differ, the frame is
refused rather than guessed: frames are the cheap thing (28-133 per dive
against `MIN_LASER_POINTS` of 2). With equal pitches (every migrated v1
target) nothing changes from v1.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.checkerboard import detection as sut

from .test_checkerboard_detection import BOARD_COLS, BOARD_ROWS, _render, _warped

PITCH_X, PITCH_Y = 0.04223, 0.04211


def _detect(img, **overrides):
    kwargs = {
        "max_rows": BOARD_ROWS,
        "max_cols": BOARD_COLS,
        "pitch_x_m": PITCH_X,
        "pitch_y_m": PITCH_Y,
    }
    kwargs.update(overrides)
    return sut.detect_checkerboard(img, **kwargs)


def _steps(detected):
    grid = detected.body_points.reshape(detected.rows, detected.cols, 2)
    along_cols = np.diff(grid[0, :, 0])  # col index -> x
    down_rows = np.diff(grid[:, 0, 1])  # row index -> y
    return along_cols, down_rows


def test_the_long_axis_of_the_e4e_board_takes_pitch_x():
    """The board's 14-corner axis is its `interior_cols` axis, whichever way
    round the detector indexed it, so that axis is spaced at `pitch_x_m`."""
    detected = _detect(_warped(_render()))
    assert detected is not None
    along_cols, down_rows = _steps(detected)

    if detected.cols == BOARD_COLS:
        assert np.allclose(along_cols, PITCH_X) and np.allclose(down_rows, PITCH_Y)
    else:
        assert detected.rows == BOARD_COLS
        assert np.allclose(along_cols, PITCH_Y) and np.allclose(down_rows, PITCH_X)


@pytest.mark.parametrize(
    ("detected", "expected"),
    [
        ((10, 14), (PITCH_X, PITCH_Y)),  # the declared orientation
        ((14, 10), (PITCH_Y, PITCH_X)),  # transposed: cols are declared rows
        ((9, 12), (PITCH_X, PITCH_Y)),  # a sub-grid only the declared way fits
        ((12, 9), (PITCH_Y, PITCH_X)),  # ... only the transposed way fits
    ],
)
def test_the_detected_shape_says_which_pitch_is_which(detected, expected):
    """`(col_pitch, row_pitch)` for the detected grid."""
    assert sut.pitch_per_detected_axis(
        *detected,
        max_rows=BOARD_ROWS,
        max_cols=BOARD_COLS,
        pitch_x_m=PITCH_X,
        pitch_y_m=PITCH_Y,
    ) == pytest.approx(expected)


@pytest.mark.parametrize("detected", [(10, 10), (3, 3), (10, 3), (5, 8)])
def test_an_ambiguous_sub_grid_is_refused_when_the_pitches_differ(detected):
    """A shape that fits the board either way round cannot say which axis is
    which, and a guess is a scale error of the anisotropy."""
    assert (
        sut.pitch_per_detected_axis(
            *detected,
            max_rows=BOARD_ROWS,
            max_cols=BOARD_COLS,
            pitch_x_m=PITCH_X,
            pitch_y_m=PITCH_Y,
        )
        is None
    )


@pytest.mark.parametrize("detected", [(10, 10), (3, 3), (10, 14), (14, 10)])
def test_equal_pitches_are_v1s_behaviour(detected):
    """Every migrated target: one pitch on both axes, any orientation."""
    assert sut.pitch_per_detected_axis(
        *detected,
        max_rows=BOARD_ROWS,
        max_cols=BOARD_COLS,
        pitch_x_m=0.0254,
        pitch_y_m=0.0254,
    ) == (0.0254, 0.0254)


def test_a_grid_that_fits_neither_way_is_refused():
    assert (
        sut.pitch_per_detected_axis(
            17,
            24,
            max_rows=BOARD_ROWS,
            max_cols=BOARD_COLS,
            pitch_x_m=0.0254,
            pitch_y_m=0.0254,
        )
        is None
    )


def test_an_ambiguous_partial_view_is_dropped_only_when_the_pitches_differ():
    """Through the detector: a cropped board detects as a sub-grid that fits
    either way; anisotropic, the frame is dropped; isotropic, it is kept."""
    board = _warped(_render())
    cropped = board[:, : int(board.shape[1] * 0.66)].copy()
    isotropic = _detect(cropped, pitch_x_m=0.0254, pitch_y_m=0.0254)
    assert isotropic is not None
    short, long = sorted((isotropic.rows, isotropic.cols))
    assert short <= min(BOARD_ROWS, BOARD_COLS) and long <= min(
        BOARD_ROWS, BOARD_COLS
    ), "the crop must detect as an ambiguous sub-grid for this test to mean it"

    assert _detect(cropped) is None
