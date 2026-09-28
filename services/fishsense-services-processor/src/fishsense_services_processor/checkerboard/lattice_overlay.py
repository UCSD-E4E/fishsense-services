"""Draw a detected checkerboard lattice onto its frame, for human verification.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/src/fishsense_data_processing_workflow_worker/lattice_overlay.py.

`checkerboard_detection` decides *that* a grid is present; this module shows a
person **which** grid, so they can answer the one question no automated check
here can: are the detected corners one board square apart, or two?

**Why that question cannot be answered in code.** A uniformly mis-latticed
detection — every corner two squares apart while `body_points` labels them one
apart — is still a perfect grid. It is therefore an exact homography of the
modelled grid, so `grid_residual_px` reads ~0 and passes it. The residual gate
catches grids whose spacing is *mixed*, which breaks regularity; the uniform
case also clears `_fits_declared_board` (a coarse lattice is *smaller* than the
declared board, so an upper bound never fires) and `check_fit_self_consistency`
(which compares 2-D dots, and a pure scale error does not move them). The pose
then places the board at a fraction of its true distance and every depth, the
fitted baseline, and every length downstream scales with it.

That is not hypothetical. Measured 2026-09-11 against the known-length targets:
five of fourteen checkerboard calibrations carry a baseline that is a simple
multiple of the consensus 10.4 cm — 4.00x, 2.16x, 2.12x, 1.51x and 0.47x — and
their dives read -75% to +45% on length. A scale error of exactly 2x or 4x is
what a coarse lattice produces and what noise does not.

**The connecting lines are the instrument, not decoration.** Corner dots alone
turn "every corner, or every other one?" into a counting exercise at high zoom.
Lines make it self-evident: a wrong lattice draws cells that visibly span two
board squares.

Kept out of `checkerboard_detection` deliberately. That module is on the
calibration path and must stay free of drawing concerns; this one is only ever
called by the verification stage.
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["draw_lattice_overlay"]

#: BGR. Magenta edges and yellow corners, because the board itself is black and
#: white and backscatter is grey-green — neither channel pair collides with the
#: thing being judged.
_EDGE_COLOR = (255, 0, 255)
_CORNER_COLOR = (0, 255, 255)
_CAPTION_COLOR = (0, 255, 255)

#: Thin enough to leave the board squares readable underneath. The labeler is
#: comparing drawn cells against printed squares, so the overlay must not cover
#: the evidence.
_EDGE_THICKNESS = 2
_CORNER_RADIUS = 4


def _as_int_point(point) -> tuple[int, int]:
    """Round to the integer pixel cv2 drawing wants.

    Corners may fall outside the frame — a board can sit partly past a
    rectified edge — and cv2 clips those itself. Rejecting or dropping them
    here would hand the labeler a bare frame, which reads as "nothing was
    detected" and is the opposite of the truth.
    """
    return int(round(float(point[0]))), int(round(float(point[1])))


def draw_lattice_overlay(
    image: np.ndarray,
    *,
    rows: int,
    cols: int,
    image_points,
    caption: str | None = None,
) -> np.ndarray:
    """Return `image` with the `rows` x `cols` lattice drawn on it.

    `image_points` is the detector's corner list in **row-major** order, the
    same order `DetectedCheckerboard.image_points` carries, so it reshapes
    directly to `(rows, cols, 2)`.

    Does not mutate `image`; the caller still holds the clean frame.
    """
    grid = np.asarray(image_points, dtype=np.float64).reshape(rows, cols, 2)
    canvas = image.copy()

    # Edges first, corners second, so a corner mark is never half-buried under
    # a line meeting it — the mark is what the labeler counts.
    #
    # Iterated as a real 2-D grid rather than over the flat list. Walking the
    # row-major list and joining consecutive entries would also connect the
    # last corner of one row to the first of the next, drawing a long diagonal
    # across the board. A labeler would read that as a lattice fault, so the
    # overlay would manufacture the very finding it exists to test for.
    for row in range(rows):
        for col in range(cols):
            if col + 1 < cols:
                cv2.line(
                    canvas,
                    _as_int_point(grid[row, col]),
                    _as_int_point(grid[row, col + 1]),
                    _EDGE_COLOR,
                    _EDGE_THICKNESS,
                    lineType=cv2.LINE_AA,
                )
            if row + 1 < rows:
                cv2.line(
                    canvas,
                    _as_int_point(grid[row, col]),
                    _as_int_point(grid[row + 1, col]),
                    _EDGE_COLOR,
                    _EDGE_THICKNESS,
                    lineType=cv2.LINE_AA,
                )

    for row in range(rows):
        for col in range(cols):
            cv2.circle(
                canvas,
                _as_int_point(grid[row, col]),
                _CORNER_RADIUS,
                _CORNER_COLOR,
                thickness=-1,
                lineType=cv2.LINE_AA,
            )

    if caption:
        # Burned in rather than carried only on the LS task: the verdict is
        # actionable only alongside the grid shape that produced it, and an
        # exported frame keeps the two together.
        cv2.putText(
            canvas,
            caption,
            (24, 56),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            _CAPTION_COLOR,
            3,
            lineType=cv2.LINE_AA,
        )

    return canvas
