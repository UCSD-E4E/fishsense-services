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
from fishsense_services_contracts.slate_calibration import (
    CheckerboardObservation,
    CheckerboardTarget,
)
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


def _board_gray(square: int = _SQUARE_PX, border: int = _BORDER) -> np.ndarray:
    height = _ROWS_SQ * square + 2 * border
    width = _COLS_SQ * square + 2 * border
    img = np.full((height, width), 255, np.uint8)
    for row in range(_ROWS_SQ):
        for col in range(_COLS_SQ):
            if (row + col) % 2 == 0:
                y_0, x_0 = border + row * square, border + col * square
                img[y_0 : y_0 + square, x_0 : x_0 + square] = 0
    return img


def _board() -> np.ndarray:
    return cv2.cvtColor(_board_gray(), cv2.COLOR_GRAY2BGR)


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


@pytest.mark.parametrize(
    "point", [[np.nan, 0.0, 1.0], [0.0, 0.0, np.inf]], ids=["nan", "inf"]
)
def test_detect_drops_a_point_that_is_not_finite(rectified_is, monkeypatch, point):
    """`fishsense_core.plane.laser_point_on_plane` checks the ray for NaN but
    not its answer; v0.48.0's own copy dropped a NaN point. A non-finite
    point never reaches the observation or the fit (review of #350)."""
    board = _board()
    rectified_is(board)
    monkeypatch.setattr(sut, "laser_point_on_plane", lambda *_: np.array(point))

    observation = sut._observe(  # pylint: disable=protected-access
        b"raw", _detect_payload(board)
    )

    assert observation.point is None
    assert observation.skip_reason == "no_ray_plane_intersection"


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


# ---------- the every-other-row lattice: a live 2x scale error ----------
#
# fishsense-core#98 found it with core's own gate (`checker_parity`): on a
# board seen at an angle, a saturated laser dot of 16 px radius or more makes
# `findChessboardCornersSB` fall back to a 7 x 10 grid of the 10 x 14 board
# that is EVERY OTHER ROW -- adjacent detected rows ~66 px apart against
# ~33 px squares. Labeled one pitch apart, that halves the board's distance
# along that axis, and so every depth the frame contributes. Its homography
# is perfect, so services' own copy of the detector (through v0.56.0)
# accepted it, and its test asserted the frame was kept. These pin the
# refusal at the call sites, on core's synthetic frame (40 px squares, an
# 80 px border, the warp below).


def _warped_core_board(radius: int) -> np.ndarray:
    """Core's `test_checkerboard.py` frame: the E4E board, warped, dotted."""
    img = _board_gray(square=40, border=80)
    height, width = img.shape
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
    img = cv2.warpPerspective(img, matrix, (width, height), borderValue=255)
    cv2.circle(img, (width // 2, height // 2), radius, 255, -1)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize("radius", [16, 20])
def test_the_frame_really_is_latticed_every_other_row(radius):
    """The precondition, so the refusals below are refusals of THIS fault.

    Raw OpenCV, called as core calls it (its requested pattern and flags,
    read from core so they cannot drift apart), on every seed core tries:
    since 4.3.1 a frame is refused only if no seed gives a passing grid, so
    on each one the grid must come back short, its row step twice its
    column step, or not at all.
    """
    from fishsense_core import (  # pylint: disable=import-outside-toplevel
        checkerboard as core,
    )

    gray = cv2.cvtColor(_warped_core_board(radius), cv2.COLOR_BGR2GRAY)
    latticed = 0
    for seed in core.DEFAULT_SEEDS:
        cv2.setRNGSeed(int(seed))
        found, corners, meta = cv2.findChessboardCornersSBWithMeta(
            gray,
            core._REQUESTED_PATTERN,  # pylint: disable=protected-access
            core._DETECTION_FLAGS,  # pylint: disable=protected-access
        )
        if not found:
            continue
        rows, cols = int(meta.shape[0]), int(meta.shape[1])
        grid = np.asarray(corners, np.float64).reshape(rows, cols, 2)
        step_rows = np.median(np.linalg.norm(np.diff(grid, axis=0), axis=2))
        step_cols = np.median(np.linalg.norm(np.diff(grid, axis=1), axis=2))
        assert step_rows / step_cols == pytest.approx(2.0, rel=0.1), seed
        latticed += 1
    assert latticed, "no seed found the every-other-row grid at all"


@pytest.mark.parametrize("radius", [16, 20])
def test_detect_refuses_a_lattice_at_twice_the_pitch(rectified_is, radius):
    """No point in space from a frame whose depth would be half the truth."""
    board = _warped_core_board(radius)
    rectified_is(board)

    observation = sut._observe(  # pylint: disable=protected-access
        b"raw", _detect_payload(board)
    )

    assert observation.point is None
    assert observation.skip_reason == "no_usable_board"


@pytest.mark.parametrize("radius", [16, 20])
def test_render_refuses_a_lattice_at_twice_the_pitch(rectified_is, radius):
    """The lattice study mirrors the fit's admission, so it drops it too."""
    board = _warped_core_board(radius)
    rectified_is(board)

    render, jpeg = sut._render(  # pylint: disable=protected-access
        b"raw", _payload(board)
    )

    assert render.skip_reason == "no_usable_board"
    assert jpeg is None


@pytest.mark.parametrize("radius", [4, 8, 12])
def test_a_smaller_dot_still_yields_a_point(rectified_is, radius):
    """The control: the gate must not eat the frames it exists to protect."""
    board = _warped_core_board(radius)
    rectified_is(board)

    observation = sut._observe(  # pylint: disable=protected-access
        b"raw", _detect_payload(board)
    )

    assert observation.skip_reason is None
    assert observation.point is not None


def test_detect_gives_the_same_observation_for_the_same_frame(rectified_is):
    """Detection is seeded (since 4.3.1, a fixed sequence of seeds): an
    occluded board used to come back as a different sub-grid per call (fishsense-core#98: 4 x 10, 5 x 3 or 3 x 3 over 20 calls),
    and with it a different outcome for the same frame (unseeded, this one
    flips between a point and `no_usable_board`)."""
    board = _board()
    height, width = board.shape[:2]
    size = 4 * _SQUARE_PX
    board[
        height // 2 - size // 2 : height // 2 + size // 2,
        width // 2 - size // 2 : width // 2 + size // 2,
    ] = 128
    rectified_is(board)
    payload = _detect_payload(board, dot=(_BORDER + 1.5 * _SQUARE_PX,) * 2)

    observations = {
        sut._observe(
            b"raw", payload
        ).model_dump_json()  # pylint: disable=protected-access
        for _ in range(10)
    }

    assert len(observations) == 1
    # And the outcome itself, so a detector that always fails cannot pass by
    # failing the same way ten times (review of #353): seeded, the occluded
    # board is found as a sub-grid beside the occluder, and the dot is off it.
    (only,) = observations
    observation = CheckerboardObservation.model_validate_json(only)
    assert observation.skip_reason == "dot_off_board"


def test_a_frame_that_is_not_8_bit_is_an_error_not_a_skip(rectified_is):
    """A 16-bit or float frame is a decode-path bug, not an unlucky frame, so
    the detector raises instead of the frame being quietly dropped as
    `no_usable_board` and the dive fitted from fewer frames."""
    board = _board().astype(np.uint16) * 257
    rectified_is(board)

    with pytest.raises(TypeError):
        sut._observe(b"raw", _detect_payload(board))  # pylint: disable=protected-access
