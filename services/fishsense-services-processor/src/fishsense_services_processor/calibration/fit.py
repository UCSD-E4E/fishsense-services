"""Fit the laser from 3-D observations, behind the four gates.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
perform_laser_calibration_activity.py (the steps after `_gather_laser_points`)
and fit_checkerboard_laser_extrinsics.py, whose own docstring says it is
"deliberately the same steps stage 13 takes once its slate observations are
in hand". v1 kept two copies; here both producers call this once. Nothing in
it knows what the target was: by now an observation is a 3-D point and the
2-D dot it came from, which is all the fit ever needed.

The steps are v1's, in v1's order:

1. refuse below `MIN_LASER_POINTS` usable observations;
2. `trim_outlying_observations` (one pass, capped);
3. `fishsense_core.laser.calibrate_laser`, origin z padded to 0;
4. the four gates, **observation geometry first** because the two projection
   gates abstain on exactly the degenerate geometry that most needs refusing
   (prod dives 347 and 107), then self-consistency, baseline plausibility,
   and whether the fit describes the dive it will measure.

v2 change: **a refusal is returned, not raised, and not recorded here.** The
processor has no database; it returns a `refused` `LaserCalibrationResult`
carrying v1's error type and message, and the orchestrator records it and
raises v1's non-retryable error. Each gate's verdict is returned too, with the
lever arm, the counts and fishsense-core's version, for the calibration row.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable

import numpy as np
from fishsense_core.laser import calibrate_laser as _calibrate_laser

from fishsense_services_contracts.calibration_bounds import MIN_LASER_POINTS
from fishsense_services_contracts.slate_calibration import LaserCalibrationResult
from fishsense_services_processor.calibration.consistency import (
    CalibrationDoesNotDescribeDiveError,
    CalibrationImplausibleError,
    CalibrationInconsistentError,
    CalibrationUnderdeterminedError,
    check_baseline_plausible,
    check_calibration_describes_dive,
    check_fit_self_consistency,
    check_observation_geometry,
)
from fishsense_services_processor.calibration.robust_fit import (
    trim_outlying_observations,
)

__all__ = ["GATES", "MIN_LASER_POINTS", "NOT_RUN", "REFUSED", "fit_laser"]

#: The gates, in the order they run, by the name the row records.
GATES = (
    "observation_geometry",
    "fit_self_consistency",
    "baseline_plausible",
    "describes_dive",
)
REFUSED = "refused"
NOT_RUN = "not_run"

_REFUSALS = (
    CalibrationUnderdeterminedError,
    CalibrationInconsistentError,
    CalibrationImplausibleError,
    CalibrationDoesNotDescribeDiveError,
)


def _core_version() -> str:
    return importlib.metadata.version("fishsense-core")


def _lever_arm(points: np.ndarray) -> float:
    """The observations' spread along the optical axis: what fixes the fitted
    direction (`check_observation_geometry`'s quantity)."""
    finite = points[np.isfinite(points).all(axis=1)] if len(points) else points
    if len(finite) < 2:
        return 0.0
    return float(finite[:, 2].max() - finite[:, 2].min())


def fit_laser(
    points,
    dots,
    camera_matrix,
    dive_dots,
    *,
    too_few_type: str,
    too_few_reason: Callable[[int, int], str],
) -> LaserCalibrationResult:
    """Fit and judge the laser from `points` (N x 3) and their `dots` (N x 2).

    `too_few_type` / `too_few_reason` are the producer's own refusal below
    `MIN_LASER_POINTS` (v1: `InsufficientLaserPoints` for the slate,
    `InsufficientCheckerboardPoints` for the board). The reason is built from
    `(count, minimum)`, so each keeps v1's wording.
    """
    observations = np.asarray(points, dtype=float).reshape(-1, 3)
    count = len(observations)
    core = _core_version()
    not_run = dict.fromkeys(GATES, NOT_RUN)

    if count < MIN_LASER_POINTS:
        # Deterministic in these observations: they are not there, and
        # re-firing will not find them. Recorded by the orchestrator, so the
        # dive leaves the cohort; it returns by itself once its labels change.
        return LaserCalibrationResult(
            outcome="refused",
            refusal_type=too_few_type,
            refusal_reason=too_few_reason(count, MIN_LASER_POINTS),
            observation_count=count,
            observations_trimmed=0,
            lever_arm_m=_lever_arm(observations),
            gate_verdicts=not_run,
            core_version=core,
        )

    # Drop observations that disagree with the ray before fitting.
    # `calibrate_laser` is plain least squares, so one badly-placed dot drags
    # the line -- and because the fit reports the z=0 crossing, a metre or
    # more behind the observations, a small angular error is levered into a
    # large baseline error. Dive 103's 6.25 cm baseline came from 17
    # otherwise clean observations.
    fitted_points = trim_outlying_observations(observations)
    trimmed = count - len(fitted_points)
    origin, orientation = _calibrate_laser(fitted_points.astype(np.float32))
    # The Rust kernel returns the origin with z=0 implicit; pad to a 3-vector.
    laser_position = np.array([float(origin[0]), float(origin[1]), 0.0])
    laser_axis = np.asarray(orientation, dtype=float)
    matrix = np.asarray(camera_matrix, dtype=float)

    verdicts = dict(not_run)
    gates = (
        # Underdetermination first: the two projection gates below abstain on
        # exactly the degenerate geometry that most needs refusing.
        ("observation_geometry", lambda: check_observation_geometry(fitted_points)),
        (
            "fit_self_consistency",
            lambda: check_fit_self_consistency(
                laser_position,
                laser_axis,
                matrix,
                np.asarray(dots, dtype=float).reshape(-1, 2),
            ),
        ),
        # Not redundant with the above: that compares the ray's projection to
        # the dots and is structurally blind to the laser's offset.
        ("baseline_plausible", lambda: check_baseline_plausible(laser_position)),
        # And the question none of the above asks: does this calibration
        # describe the dive it will measure? A mid-dive re-seat leaves the fit
        # consistent with its own burst and wrong for every frame after it.
        (
            "describes_dive",
            lambda: check_calibration_describes_dive(
                laser_position,
                laser_axis,
                matrix,
                np.asarray(dive_dots, dtype=float).reshape(-1, 2),
            ),
        ),
    )
    for name, gate in gates:
        try:
            verdicts[name] = gate()
        except _REFUSALS as exc:
            verdicts[name] = REFUSED
            return LaserCalibrationResult(
                outcome="refused",
                refusal_type=type(exc).__name__,
                refusal_reason=str(exc),
                observation_count=count,
                observations_trimmed=trimmed,
                lever_arm_m=_lever_arm(fitted_points),
                gate_verdicts=verdicts,
                core_version=core,
            )

    return LaserCalibrationResult(
        outcome="accepted",
        laser_position=laser_position.tolist(),
        laser_axis=laser_axis.tolist(),
        observation_count=count,
        observations_trimmed=trimmed,
        lever_arm_m=_lever_arm(fitted_points),
        gate_verdicts=verdicts,
        core_version=core,
    )
