"""Stage 13: fit the laser from a dive's slate-laser observations.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
perform_laser_calibration_activity.py (itself a port of
`scripts/stage13_perform_laser_calibration.ipynb`). The Atanasov fit is
`fishsense_core.laser.calibrate_laser`; the pose and the ray-plane
intersection are `calibration.geometry`; the fit and its gates are
`calibration.fit`.

What remains slate-specific here is only how the correspondences are
obtained: template points scaled by the slate's DPI, paired against the
labeler's hand-placed reference points. The geometry from there on is shared
with the checkerboard producer.

v2 changes:

* **the processor reads and writes nothing.** v1's activity fetched the dive,
  the slate, its labels, each label's laser dot, the intrinsics and the
  dive's dots through the SDK, and PUT the extrinsics. The orchestrator now
  resolves all of that into a `SlateCalibrationInput`, and this returns a
  `LaserCalibrationResult` for it to persist;
* a refusal is **returned** (`outcome="refused"`, v1's error type and
  message); the orchestrator records it and raises v1's non-retryable error;
* each observation carries its own laser dot. v1 took
  `get_laser_label(image_id).first()` with no ordering; the orchestrator now
  takes the lowest live label deterministically (the checkerboard resolver's
  rule), so a re-dispatch fits the same points.
"""

from __future__ import annotations

import numpy as np
from temporalio import activity

from fishsense_services_contracts.slate_calibration import (
    LaserCalibrationResult,
    SlateCalibrationInput,
    SlateObservation,
)
from fishsense_services_processor.calibration.fit import MIN_LASER_POINTS, fit_laser
from fishsense_services_processor.calibration.geometry import (
    laser_point_on_plane,
    plane_from_correspondences,
)

__all__ = ["INCH_TO_M", "MIN_LASER_POINTS", "perform_laser_calibration"]

INCH_TO_M = 0.0254


def _drop_skipped(points: list, skipped) -> list:
    """Remove `skipped` indices from `points`, resolved against the ORIGINAL list.

    A sequential ``list.pop(idx)`` (the previous implementation, and the
    stage-13 notebook) is only correct for <=1 skipped point: popping index 2
    slides every later element down one, so a subsequent ``pop(5)`` removes what
    was originally index 6 — feeding a wrong 3D<->2D pair into solvePnP. Latent
    while every production label skips <=1 point, but model-assisted labeling
    emits per-point visibility (multi-skip), so it activates the moment that
    ships. Resolve all indices up front, and reject duplicate / out-of-range
    indices.
    """
    indices = [int(i) for i in skipped]
    if len(set(indices)) != len(indices):
        raise ValueError(f"duplicate skipped index in {indices}")
    for i in indices:
        if not 0 <= i < len(points):
            raise ValueError(f"skipped index {i} out of range for {len(points)} points")
    drop = set(indices)
    return [p for i, p in enumerate(points) if i not in drop]


def _laser_point_in_camera_space(
    observation: SlateObservation,
    template_points,
    dpi: int,
    camera_matrix,
) -> np.ndarray | None:
    """Lift one slate-laser observation to a 3-D point in camera space.

    Returns None when the observation can't be used (a count mismatch, PnP
    failure, NaN ray).
    """
    source_points = _drop_skipped(
        list(template_points or []), observation.skipped_points or []
    )
    image_points = list(observation.reference_points or [])
    # solvePnP pairs body<->image points purely by position, so a count that
    # disagrees with (template - skipped) silently mis-pairs the geometry.
    # Never pair those.
    #
    # Skip the LABEL, not the dive. This used to raise, which took the whole
    # dive down: prod dive 526 holds 17 completed slate labels of which 2
    # carry a JSON `null` for `reference_points`, and those 2 discarded the 15
    # good observations. The bare `ValueError` also bypassed the refusal
    # record, so the dive stayed in the cohort and head-of-line blocked it --
    # 10 consecutive hourly failures on 2026-09-12.
    #
    # Returning None keeps the pairing guarantee and routes a dive left with
    # too few observations into the MIN_LASER_POINTS refusal, which is
    # recorded. A mismatch is a property of one label's annotation, so one
    # bad label must not decide the dive.
    if len(image_points) != len(source_points):
        activity.logger.warning(
            "skipping slate observation capture=%s: reference_points=%d "
            "disagrees with the template (%d) minus %d skipped = %d",
            observation.capture_id,
            len(image_points),
            len(template_points or []),
            len(set(int(i) for i in (observation.skipped_points or []))),
            len(source_points),
        )
        return None
    # The slate's whole contribution: template points in metres. Everything
    # after `plane_from_correspondences` is target-agnostic and shared with the
    # checkerboard producer -- see `calibration.geometry`.
    body_points = (np.array(source_points) / float(dpi)) * INCH_TO_M

    matrix = np.asarray(camera_matrix, dtype=float)
    plane = plane_from_correspondences(body_points, np.array(image_points), matrix)
    if plane is None:
        return None

    return laser_point_on_plane(
        plane, np.array([observation.laser_x, observation.laser_y]), matrix
    )


def _gather_laser_points(
    payload: SlateCalibrationInput,
) -> tuple[list[np.ndarray], list[tuple[float, float]]]:
    """Lift each usable slate-laser observation to camera space.

    Returns `(laser_points_3d, laser_dots_2d)` in lockstep — the 2D pixels
    feed the self-consistency gate (the fitted ray must reproject onto the
    very dots it was computed from)."""
    laser_points: list[np.ndarray] = []
    laser_dots: list[tuple[float, float]] = []
    for observation in payload.observations:
        point = _laser_point_in_camera_space(
            observation, payload.template_points, payload.dpi, payload.camera_matrix
        )
        if point is not None:
            laser_points.append(point)
            laser_dots.append((float(observation.laser_x), float(observation.laser_y)))
    return laser_points, laser_dots


@activity.defn(name="perform_laser_calibration")
async def perform_laser_calibration(
    payload: SlateCalibrationInput,
) -> LaserCalibrationResult:
    """Fit the dive's laser from its slate observations, or say why not.

    Every refusal is deterministic in the observations this run was
    dispatched with (or, for the describes-the-dive gate, in the dive's dots,
    which only relabelling changes), so the orchestrator records it and raises
    non-retryably, as v1 did.
    """
    laser_points, laser_dots = _gather_laser_points(payload)
    result = fit_laser(
        laser_points,
        laser_dots,
        payload.camera_matrix,
        payload.dive_dots,
        too_few_type="InsufficientLaserPoints",
        too_few_reason=lambda count, minimum: (
            f"insufficient laser points ({count} < {minimum})"
        ),
    )
    if result.observations_trimmed:
        activity.logger.info(
            "dive_id=%s: trimmed %d of %d laser observations as outliers",
            payload.dive_id,
            result.observations_trimmed,
            result.observation_count,
        )
    activity.logger.info(
        "stage 13 dive_id=%s outcome=%s observations=%d verdicts=%s",
        payload.dive_id,
        result.outcome,
        result.observation_count,
        result.gate_verdicts,
    )
    return result
