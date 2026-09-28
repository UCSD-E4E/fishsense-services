"""Checkerboard calibration and lattice verification: the per-frame and fit
activities.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
(detect_checkerboard_laser_point.py, fit_checkerboard_laser_extrinsics.py,
render_checkerboard_lattice.py) and the per-image DTOs from
workflows/perform_checkerboard_calibration_workflow.py and
verify_checkerboard_lattice_workflow.py.

**Detect** lifts one frame's laser dot to a 3-D point in camera space:
download the staged raw `.ORF`, rectify it, find the board, check the dot is
on it, and intersect the dot's ray with the board's plane. The result is
exactly what stage 13 produces from a slate. Per-image, on the per-image
role: each frame is a rawpy decode peaking at 1-3 GB.

**It rectifies rather than reusing the stage-0.1 JPEG** (v1): that JPEG has a
green laser-region outline drawn across it and has been through JPEG
quantisation, either of which can sit on the board and move a corner; corner
positions are the entire input to the pose.

**Fit** is `calibration.fit`, the steps stage 13 takes once its observations
are in hand. Nothing in it knows the target was a board.

**Render** mirrors detect's admission filter exactly -- `detect_checkerboard`,
then `board_hull` + `point_in_laser_region` -- because the study asks what the
*fit* consumed, so its population has to be the fit's; then draws the lattice
and uploads it for a person to judge.

v2 changes:

* the processor reads and writes only the `ObjectRef`s it is handed (v1:
  keys built from checksums), and returns results rather than writing rows:
  the fit returns a `LaserCalibrationResult` -- a refusal included -- for the
  orchestrator to record (v1 recorded it through the SDK and raised);
* the dive's dots arrive in the fit's payload (v1: fetched there, before the
  try, so a transport error stayed retryable -- with no fetch there is
  nothing to keep retryable);
* the board carries its pitch per axis (`CheckerboardTarget`);
* the JPEG encoder is a local copy of v1's `encode_rectified_jpeg` (the
  head/tail slice owns its port).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from pathlib import Path
from typing import List
from uuid import UUID

import cv2
import numpy as np
from fishsense_core.camera_intrinsics import CameraIntrinsics
from fishsense_core.image.raw_image import RawImage
from fishsense_core.image.rectified_image import RectifiedImage
from pydantic import BaseModel
from temporalio import activity

from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_contracts.slate_calibration import (
    CheckerboardLatticeRender,
    CheckerboardObservation,
    CheckerboardTarget,
    LaserCalibrationResult,
    Point,
)
from fishsense_services_processor.calibration.fit import fit_laser
from fishsense_services_processor.calibration.geometry import (
    laser_point_on_plane,
    plane_from_correspondences,
)
from fishsense_services_processor.calibration.region import point_in_laser_region
from fishsense_services_processor.checkerboard.detection import (
    board_hull,
    detect_checkerboard,
    median_corner_spacing,
)
from fishsense_services_processor.checkerboard.lattice_overlay import (
    draw_lattice_overlay,
)
from fishsense_services_processor.object_store import ProcessorObjectStore

__all__ = [
    "DetectCheckerboardLaserPointInput",
    "FitCheckerboardExtrinsicsInput",
    "RenderCheckerboardLatticeInput",
    "detect_checkerboard_laser_point",
    "encode_jpeg",
    "fit_checkerboard_laser_extrinsics",
    "open_store",
    "render_checkerboard_lattice",
]


class DetectCheckerboardLaserPointInput(BaseModel):
    """Per-image payload for `detect_checkerboard_laser_point`."""

    capture_id: UUID
    raw: ObjectRef
    laser_x: float
    laser_y: float
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    #: The declared board: its INTERIOR corners are an upper bound on what may
    #: be detected, not the grid asked for. See `checkerboard.detection`.
    target: CheckerboardTarget


class FitCheckerboardExtrinsicsInput(BaseModel):
    """Dive-level payload for `fit_checkerboard_laser_extrinsics`."""

    dive_id: UUID
    camera_matrix: List[List[float]]
    observations: List[CheckerboardObservation]
    dive_dots: List[Point]


class RenderCheckerboardLatticeInput(BaseModel):
    """Per-image payload for `render_checkerboard_lattice`."""

    capture_id: UUID
    raw: ObjectRef
    render: ObjectRef
    laser_x: float
    laser_y: float
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    target: CheckerboardTarget


def open_store() -> ProcessorObjectStore:
    """The object store, from ``FISHSENSE_OBJECT_STORE_*`` (tests replace it)."""
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


def encode_jpeg(image_bgr: np.ndarray) -> bytes:
    """Encode a BGR ndarray to JPEG bytes. Does not mutate. (v1's
    `encode_rectified_jpeg`.)"""
    success, encoded = cv2.imencode(".jpg", image_bgr)
    if not success:
        raise RuntimeError("cv2.imencode failed")
    return encoded.tobytes()


def _rectified(raw, camera_matrix, distortion_coefficients) -> np.ndarray:
    # Rectified, because `plane_from_correspondences` passes zero distortion
    # to solvePnP. Detecting on raw pixels yields a plausible, slightly wrong
    # pose and no error.
    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion_coefficients=np.array(distortion_coefficients, dtype=float),
    )
    return RectifiedImage(RawImage(raw), intrinsics).data


def _detect(image: np.ndarray, target: CheckerboardTarget):
    return detect_checkerboard(
        image,
        max_rows=target.rows,
        max_cols=target.cols,
        pitch_x_m=target.pitch_x_m,
        pitch_y_m=target.pitch_y_m,
    )


async def _with_raw(ref: ObjectRef, work):
    """Download the staged raw frame to a scratch file and run `work(path)`
    off the event loop; the file is gone when this returns."""
    store = open_store()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = await store.download_raw(ref, Path(tmpdir))
        return store, await asyncio.to_thread(work, path)


# --- detect -------------------------------------------------------------------


def _observe(
    raw, payload: DetectCheckerboardLaserPointInput
) -> CheckerboardObservation:
    """Sync helper run via `asyncio.to_thread` — decode, undistort, PnP.

    Returns an observation with `point=None` for any frame that cannot be
    used, rather than raising. A dive is fitted from dozens of frames and only
    needs `MIN_LASER_POINTS` of them, so one unreadable board is ordinary; a
    raise here would fail the whole dive over it.
    """
    image = _rectified(raw, payload.camera_matrix, payload.distortion_coefficients)

    def unusable(reason: str) -> CheckerboardObservation:
        return CheckerboardObservation(
            capture_id=payload.capture_id,
            point=None,
            laser_x=payload.laser_x,
            laser_y=payload.laser_y,
            skip_reason=reason,
        )

    detected = _detect(image, payload.target)
    if detected is None:
        return unusable("no_usable_board")

    # **Was the dot actually ON the board?** Everything downstream assumes so
    # and none of it can check: `laser_point_on_plane` intersects the camera
    # ray with the board's *infinite* plane, so a dot that missed and landed
    # on whatever was behind gets a confident, wrong depth, and
    # `check_fit_self_consistency` compares 2-D dots that are identical either
    # way. This is the checkerboard's equivalent of the `Slate, Laser on
    # slate` marker, decided from the frame rather than by a labeler.
    #
    # The hull is the detected grid's outline, so occlusion helps: an object
    # in front of the board removes the corners under it, the detector falls
    # back to a rectangle beside it, and a dot on the object lands outside.
    if not point_in_laser_region(
        float(payload.laser_x), float(payload.laser_y), board_hull(detected)
    ):
        return unusable("dot_off_board")

    matrix = np.array(payload.camera_matrix, dtype=float)
    plane = plane_from_correspondences(
        detected.body_points, detected.image_points, matrix
    )
    if plane is None:
        return unusable("no_pose")

    point = laser_point_on_plane(
        plane, np.array([payload.laser_x, payload.laser_y], dtype=float), matrix
    )
    if point is None:
        return unusable("no_ray_plane_intersection")

    return CheckerboardObservation(
        capture_id=payload.capture_id,
        point=[float(point[0]), float(point[1]), float(point[2])],
        laser_x=payload.laser_x,
        laser_y=payload.laser_y,
        detected_rows=detected.rows,
        detected_cols=detected.cols,
    )


@activity.defn(name="detect_checkerboard_laser_point")
async def detect_checkerboard_laser_point(
    payload: DetectCheckerboardLaserPointInput,
) -> CheckerboardObservation:
    """Where this frame's laser dot sits in space, or why it can't say."""
    _, observation = await _with_raw(payload.raw, lambda path: _observe(path, payload))
    if observation.point is None:
        activity.logger.info(
            "no usable checkerboard observation capture=%s reason=%s",
            payload.capture_id,
            observation.skip_reason,
        )
    else:
        activity.logger.info(
            "checkerboard observation capture=%s grid=%dx%d depth=%.3fm",
            payload.capture_id,
            observation.detected_rows,
            observation.detected_cols,
            observation.point[2],
        )
    return observation


# --- fit ----------------------------------------------------------------------


def _usable(
    observations: List[CheckerboardObservation],
) -> tuple[list[list[float]], list[tuple[float, float]]]:
    """Split the observations that produced a point into points and dots.

    Returned in lockstep: the 2-D dots feed the self-consistency gate, which
    asks whether the fitted ray reprojects onto the very dots it came from.
    """
    points: list[list[float]] = []
    dots: list[tuple[float, float]] = []
    for observation in observations:
        if observation.point is None:
            continue
        points.append([float(value) for value in observation.point])
        dots.append((float(observation.laser_x), float(observation.laser_y)))
    return points, dots


@activity.defn(name="fit_checkerboard_laser_extrinsics")
async def fit_checkerboard_laser_extrinsics(
    payload: FitCheckerboardExtrinsicsInput,
) -> LaserCalibrationResult:
    """Fit the dive's laser from its board observations, or say why not.

    Refuses when fewer than `MIN_LASER_POINTS` frames yielded a usable
    observation. That is a real data problem worth surfacing — the cohort
    promised at least that many laser-dotted frames, so falling short means
    the boards themselves were not found, and no amount of re-firing will
    change that. The reason carries the per-reason tally of the dropped
    frames, because the remedy differs by reason.
    """
    points, dots = _usable(payload.observations)
    skipped = dict(
        sorted(
            Counter(
                o.skip_reason or "unknown"
                for o in payload.observations
                if o.point is None
            ).items()
        )
    )
    activity.logger.info(
        "checkerboard calibration dive_id=%s usable=%d of %d observations "
        "skipped=%s",
        payload.dive_id,
        len(points),
        len(payload.observations),
        skipped or "{}",
    )
    result = fit_laser(
        points,
        dots,
        payload.camera_matrix,
        payload.dive_dots,
        too_few_type="InsufficientCheckerboardPoints",
        too_few_reason=lambda count, minimum: (
            f"insufficient checkerboard laser points ({count} < {minimum}) "
            f"from {len(payload.observations)} frames; skipped={skipped}"
        ),
    )
    if result.observations_trimmed:
        activity.logger.info(
            "dive_id=%s: trimmed %d of %d checkerboard observations as outliers",
            payload.dive_id,
            result.observations_trimmed,
            result.observation_count,
        )
    return result


# --- render -------------------------------------------------------------------


def _unusable_render(payload, reason: str):
    return (
        CheckerboardLatticeRender(capture_id=payload.capture_id, skip_reason=reason),
        None,
    )


def _render(
    raw, payload: RenderCheckerboardLatticeInput
) -> tuple[CheckerboardLatticeRender, bytes | None]:
    """Sync helper run via `asyncio.to_thread` — decode, undistort, detect, draw.

    Returns `(render, jpeg_bytes)`, with `jpeg_bytes` None for a frame that
    produced no lattice. Never raises on an unusable frame: a dive holds dozens
    and one unreadable board is ordinary, so a raise would fail the whole dive
    over it.
    """
    image = _rectified(raw, payload.camera_matrix, payload.distortion_coefficients)

    detected = _detect(image, payload.target)
    if detected is None:
        return _unusable_render(payload, "no_usable_board")

    if not point_in_laser_region(
        float(payload.laser_x), float(payload.laser_y), board_hull(detected)
    ):
        return _unusable_render(payload, "dot_off_board")

    spacing = median_corner_spacing(detected.image_points, detected.rows, detected.cols)
    height, width = image.shape[:2]
    overlaid = draw_lattice_overlay(
        image,
        rows=detected.rows,
        cols=detected.cols,
        image_points=detected.image_points,
        caption=f"{detected.rows}x{detected.cols}  spacing {spacing:.1f}px",
    )

    return (
        CheckerboardLatticeRender(
            capture_id=payload.capture_id,
            image=payload.render,
            detected_rows=detected.rows,
            detected_cols=detected.cols,
            median_spacing_px=float(spacing),
            # Rounded to 1/100 px, which more than halves the payload: a full
            # 10x14 render goes from ~5.6 KB to ~2.6 KB, clear of Temporal's
            # 2 MB blob limit for a large dive. Safe because these corners
            # never reach any geometry: the fit runs its own detection.
            corners=[
                [round(float(x), 2), round(float(y), 2)]
                for x, y in detected.image_points
            ],
            width=int(width),
            height=int(height),
        ),
        encode_jpeg(overlaid),
    )


@activity.defn(name="render_checkerboard_lattice")
async def render_checkerboard_lattice(
    payload: RenderCheckerboardLatticeInput,
) -> CheckerboardLatticeRender:
    """Draw this frame's detected lattice and upload it, or say why it has none."""
    store, (render, jpeg_bytes) = await _with_raw(
        payload.raw, lambda path: _render(path, payload)
    )

    if jpeg_bytes is None:
        activity.logger.info(
            "no lattice to render capture=%s reason=%s",
            payload.capture_id,
            render.skip_reason,
        )
        return render

    await store.upload_processed_jpeg(payload.render, jpeg_bytes)
    activity.logger.info(
        "rendered lattice capture=%s grid=%dx%d spacing=%.1fpx",
        payload.capture_id,
        render.detected_rows,
        render.detected_cols,
        render.median_spacing_px,
    )
    return render
