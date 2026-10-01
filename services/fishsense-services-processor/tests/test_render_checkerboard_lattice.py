"""Unit tests for the per-frame lattice render, and the per-frame detect.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_render_checkerboard_lattice.py. Fast counterparts
to v1's lattice integration tests: no Temporal, no object store, no rawpy.
What they cover is the *admission* branches and the payload shape, both of
which decide what a labeler ends up judging.

`dot_off_board` is the branch that matters most: it keeps the study's
population the fit's population.

v2 changes: frames are captures with a raw `ObjectRef`, the render is written
to the `ObjectRef` the orchestrator issued (v1: a folder and checksum), and the
render DTO says where it went; the board carries its pitch per axis. v1 had no
unit test of `detect_checkerboard_laser_point` (only its integration test,
which needs a real `.ORF`); its admission branches are the render's, so they
are pinned here the same way.
"""

from __future__ import annotations

import uuid

import cv2
import numpy as np
import pytest

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import CheckerboardTarget
from fishsense_services_processor.checkerboard import activities as sut
from fishsense_services_processor.checkerboard.activities import (
    DetectCheckerboardLaserPointInput,
    RenderCheckerboardLatticeInput,
)

_K = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]

# The E4E board: 15 x 11 squares -> 14 x 10 interior corners.
_COLS_SQ, _ROWS_SQ = 15, 11
_COLS, _ROWS = _COLS_SQ - 1, _ROWS_SQ - 1
_SQUARE_PX = 60
_BORDER = 120
_TENANT = "7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11"
_RAW = ObjectRef(bucket="fishsense-lite", key=f"tenants/{_TENANT}/raw/{'c' * 32}.ORF")
_RENDER = ObjectRef(
    bucket="labels",
    key=f"tenants/{_TENANT}/checkerboard_lattice_jpeg/{'c' * 32}.JPG",
)
_TARGET = CheckerboardTarget(rows=_ROWS, cols=_COLS, pitch_x_m=0.042, pitch_y_m=0.042)


def _board() -> np.ndarray:
    height = _ROWS_SQ * _SQUARE_PX + 2 * _BORDER
    width = _COLS_SQ * _SQUARE_PX + 2 * _BORDER
    img = np.full((height, width), 255, np.uint8)
    for row in range(_ROWS_SQ):
        for col in range(_COLS_SQ):
            if (row + col) % 2 == 0:
                y_0, x_0 = _BORDER + row * _SQUARE_PX, _BORDER + col * _SQUARE_PX
                img[y_0 : y_0 + _SQUARE_PX, x_0 : x_0 + _SQUARE_PX] = 0
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _blank() -> np.ndarray:
    return np.full((800, 1000, 3), 200, np.uint8)


@pytest.fixture
def rectified_is(monkeypatch):
    """Substitute the decode so these stay unit tests.

    Only the decode: the detector, the overlay, the spacing and the DTO
    assembly below are all production code.
    """

    def _install(image: np.ndarray):
        class _FakeRectified:  # pylint: disable=too-few-public-methods
            def __init__(self, _raw, _intrinsics):
                self.data = image

        monkeypatch.setattr(sut, "RectifiedImage", _FakeRectified)
        monkeypatch.setattr(sut, "RawImage", lambda raw: raw)

    return _install


def _dot(image, dot):
    height, width = image.shape[:2]
    return dot if dot is not None else (width / 2, height / 2)


def _payload(image: np.ndarray, *, dot=None) -> RenderCheckerboardLatticeInput:
    dot_x, dot_y = _dot(image, dot)
    return RenderCheckerboardLatticeInput(
        capture_id=uuid.UUID(int=11),
        raw=_RAW,
        render=_RENDER,
        laser_x=dot_x,
        laser_y=dot_y,
        camera_matrix=_K,
        distortion_coefficients=[0.0] * 5,
        target=_TARGET,
    )


def _detect_payload(
    image: np.ndarray, *, dot=None
) -> DetectCheckerboardLaserPointInput:
    dot_x, dot_y = _dot(image, dot)
    return DetectCheckerboardLaserPointInput(
        capture_id=uuid.UUID(int=11),
        raw=_RAW,
        laser_x=dot_x,
        laser_y=dot_y,
        camera_matrix=_K,
        distortion_coefficients=[0.0] * 5,
        target=_TARGET,
    )


def test_a_board_with_the_dot_on_it_renders(rectified_is):
    board = _board()
    rectified_is(board)

    render, jpeg = sut._render(
        b"raw", _payload(board)
    )  # pylint: disable=protected-access

    assert render.skip_reason is None
    assert render.image == _RENDER
    assert jpeg is not None and jpeg[:2] == b"\xff\xd8"
    assert sorted((render.detected_rows, render.detected_cols)) == sorted(
        (_ROWS, _COLS)
    )


def test_a_dot_off_the_board_is_refused_and_uploads_nothing(rectified_is):
    """It keeps this study's population matched to the fit's: the calibration
    path refuses the same frames, so rendering one here would put a frame in
    front of a labeler that never contributed to the calibration under test.
    """
    board = _board()
    rectified_is(board)

    # Top-left corner, comfortably outside the detected grid's hull.
    render, jpeg = sut._render(  # pylint: disable=protected-access
        b"raw", _payload(board, dot=(2.0, 2.0))
    )

    assert render.skip_reason == "dot_off_board"
    assert jpeg is None
    assert render.corners is None
    assert render.image is None


def test_a_frame_with_no_board_is_refused_and_uploads_nothing(rectified_is):
    blank = _blank()
    rectified_is(blank)

    render, jpeg = sut._render(
        b"raw", _payload(blank)
    )  # pylint: disable=protected-access

    assert render.skip_reason == "no_usable_board"
    assert jpeg is None


def test_corners_are_rounded_to_hundredths_of_a_pixel(rectified_is):
    """A payload-size safeguard that nothing else would notice losing.

    Full float64 corners more than double the DTO — ~5.6 KB against ~2.6 KB for
    a 10x14 render — which puts a large dive's workflow result near Temporal's
    2 MB blob limit instead of comfortably under it. Safe to round because
    these corners never reach any geometry: the fit runs its own detection and
    never reads this DTO.
    """
    board = _board()
    rectified_is(board)

    render, _ = sut._render(b"raw", _payload(board))  # pylint: disable=protected-access

    corners = list(render.corners or [])
    assert corners
    for x, y in corners:
        assert x == round(x, 2)
        assert y == round(y, 2)


def test_the_render_reports_the_frame_it_actually_drew_on(rectified_is):
    """Dimensions must describe the rendered frame.

    Label Studio keypoints are percentages of the image, so a mismatch here
    scatters every mark — and a labeler would report that as a lattice fault,
    which is precisely the answer this study must not manufacture.
    """
    board = _board()
    rectified_is(board)

    render, jpeg = sut._render(
        b"raw", _payload(board)
    )  # pylint: disable=protected-access
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

    assert (render.width, render.height) == (decoded.shape[1], decoded.shape[0])
    assert (render.width, render.height) == (board.shape[1], board.shape[0])


def test_spacing_is_reported_and_matches_the_drawn_square(rectified_is):
    """The machine-readable counterpart of the labeler's verdict.

    A lattice at twice the true pitch reports twice the spacing, so the two
    answers can be checked against each other afterwards.
    """
    board = _board()
    rectified_is(board)

    render, _ = sut._render(b"raw", _payload(board))  # pylint: disable=protected-access

    assert render.median_spacing_px == pytest.approx(_SQUARE_PX, rel=0.02)


# ---------- the detect activity's admission, the same filter ----------


def test_a_dot_on_a_board_becomes_a_point_in_space(rectified_is):
    board = _board()
    rectified_is(board)

    observation = sut._observe(
        b"raw", _detect_payload(board)
    )  # pylint: disable=protected-access

    assert observation.skip_reason is None
    assert observation.point is not None and len(observation.point) == 3
    assert observation.point[2] > 0
    assert sorted((observation.detected_rows, observation.detected_cols)) == sorted(
        (_ROWS, _COLS)
    )


def test_detect_drops_a_dot_off_the_board(rectified_is):
    """A dot that missed the board gets a confident, wrong depth from the
    board's infinite plane; the frame is dropped instead."""
    board = _board()
    rectified_is(board)

    observation = sut._observe(  # pylint: disable=protected-access
        b"raw", _detect_payload(board, dot=(2.0, 2.0))
    )

    assert observation.point is None
    assert observation.skip_reason == "dot_off_board"


def test_detect_drops_a_frame_with_no_board(rectified_is):
    blank = _blank()
    rectified_is(blank)

    observation = sut._observe(
        b"raw", _detect_payload(blank)
    )  # pylint: disable=protected-access

    assert observation.point is None
    assert observation.skip_reason == "no_usable_board"
    assert (observation.laser_x, observation.laser_y) == (500.0, 400.0)


class _FakeStore:
    def __init__(self) -> None:
        self.uploaded: list[tuple[ObjectRef, bytes]] = []
        self.downloaded: list[ObjectRef] = []

    async def download_raw(self, ref, directory):
        self.downloaded.append(ref)
        path = directory / "frame.ORF"
        path.write_bytes(b"raw")
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.uploaded.append((ref, data))


async def test_the_render_activity_reads_the_raw_and_writes_where_it_was_told(
    rectified_is, monkeypatch
):
    """v2: the processor reads and writes exactly the refs it was handed."""
    from temporalio.testing import ActivityEnvironment

    board = _board()
    rectified_is(board)
    store = _FakeStore()
    monkeypatch.setattr(sut, "open_store", lambda: store)

    render = await ActivityEnvironment().run(
        sut.render_checkerboard_lattice, _payload(board)
    )

    assert store.downloaded == [_RAW]
    assert [ref for ref, _ in store.uploaded] == [_RENDER]
    assert render.image == _RENDER


async def test_a_skipped_render_writes_nothing(rectified_is, monkeypatch):
    from temporalio.testing import ActivityEnvironment

    rectified_is(_blank())
    store = _FakeStore()
    monkeypatch.setattr(sut, "open_store", lambda: store)

    render = await ActivityEnvironment().run(
        sut.render_checkerboard_lattice, _payload(_blank())
    )

    assert render.skip_reason == "no_usable_board"
    assert store.uploaded == []
