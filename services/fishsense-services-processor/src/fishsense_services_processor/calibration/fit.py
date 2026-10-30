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

v2 change (fishsense-core 4.3.0, core #99): the trim and the four gates are
fishsense-core's (`fishsense_core.calibration_checks`), ported there from this
package's `calibration/robust_fit.py` and `calibration/consistency.py`, which
are gone. What stays here is pipeline policy: the order above, services' own
`MIN_LASER_POINTS`, the contract's baseline bounds (passed explicitly, because
the api excludes stored calibrations on the same numbers), and
`REFUSAL_REMEDIES`, the operator text core no longer carries -- passed to
every gate, so stored refusal messages are byte-identical to before the move
(`tests/test_calibration_refusal_messages.py`). Two departures come with
core's versions: a non-finite observation is dropped instead of being fitted,
and a malformed array raises `TypeError` rather than a refusal. `fit_laser`
reshapes its inputs, so the second cannot reach it.

A non-finite observation (a failed pose or depth) is not an observation, so
`fit_laser` drops it with its dot before anything counts (review of #351):
too few real observations get the producer's own refusal rather than a bare
`ValueError` from the kernel or the geometry gate's, `observation_count`
counts only real ones, and `observations_trimmed` stays the outlier count the
producers log it as. Neither producer hands the fit such a row today.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable

import numpy as np
from fishsense_core.calibration_checks import (
    CalibrationDoesNotDescribeDiveError,
    CalibrationImplausibleError,
    CalibrationInconsistentError,
    CalibrationUnderdeterminedError,
    check_baseline_plausible,
    check_calibration_describes_mission,
    check_fit_self_consistency,
    check_observation_geometry,
    trim_outlying_observations,
)
from fishsense_core.laser import calibrate_laser as _calibrate_laser

from fishsense_services_contracts.calibration_bounds import (
    MAX_BASELINE_M,
    MIN_BASELINE_M,
    MIN_LASER_POINTS,
)
from fishsense_services_contracts.slate_calibration import LaserCalibrationResult

__all__ = [
    "GATES",
    "MIN_LASER_POINTS",
    "NOT_RUN",
    "REFUSAL_REMEDIES",
    "REFUSED",
    "fit_laser",
]

#: The only thing an operator sees when a fit is refused: it is stored as the
#: refused row's `refusal_reason` and read back from prod. One string, passed
#: to every gate as `remedies=`, so the gates cannot drift on the remedies they
#: name. Moved here from `calibration/consistency.py` when the gates moved to
#: fishsense-core (4.3.0), which takes the remedy text from its caller.
#:
#: **Do not reintroduce the claim that the dive is re-selected hourly.** This
#: text used to say so, and it was made false by the refusal record: the
#: refusal is appended to `laser_calibrations`, and both calibration cohorts
#: then exclude the dive until one of its labels is newer than the refusal.
#: Five prod dives still carry the stale sentence, and it was later read back
#: off one of them as a live diagnosis of a churn that was not happening. A
#: message that describes the wrong failure mode costs more than no message.
#:
#: v2: v1 named `Priority.NONE` and a v1 API route; these are v2's levers.
REFUSAL_REMEDIES = (
    "The refusal is recorded, so this dive has left the calibration cohort "
    "and nothing is retrying it. It returns on its own once any laser or "
    "slate label on it is newer than the refusal, or its slate template or "
    "calibration target changes, so relabelling is enough and there is no "
    "flag to clear. If its observations cannot be fixed, park it with "
    "priority 'none' and a note; for a checkerboard dive that needs "
    "different frames rather than better labels, clear its "
    "calibration_target_id."
)

#: The gates, in the order they run, by the name the row records.
GATES = (
    "observation_geometry",
    "fit_self_consistency",
    "baseline_plausible",
    "describes_dive",
)
REFUSED = "refused"
NOT_RUN = "not_run"

_log = logging.getLogger(__name__)

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
    direction (`check_observation_geometry`'s quantity). Core keeps its own
    copy inside the gate and does not expose it;
    `test_the_recorded_lever_arm_is_the_one_the_gate_judges` pins the two
    together."""
    finite = points[np.isfinite(points).all(axis=1)] if len(points) else points
    if len(finite) < 2:
        return 0.0
    return float(finite[:, 2].max() - finite[:, 2].min())


def _kept(observations: np.ndarray, fitted: np.ndarray) -> np.ndarray:
    """Which rows of `observations` the trim kept, as a mask. Core's trim
    returns the kept rows themselves, in order, so they are matched back."""
    kept = np.zeros(len(observations), dtype=bool)
    j = 0
    for i, row in enumerate(observations):
        if j < len(fitted) and np.array_equal(row, fitted[j]):
            kept[i] = True
            j += 1
    if j != len(fitted):
        raise AssertionError("the trim returned rows it was not given")
    return kept


def fit_laser(
    points,
    dots,
    camera_matrix,
    mission_dots,
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
    given = np.asarray(points, dtype=float).reshape(-1, 3)
    finite = np.isfinite(given).all(axis=1)
    if not finite.all():
        _log.warning(
            "dropping %d non-finite of %d laser observations before the fit",
            int((~finite).sum()),
            len(given),
        )
    observations = given[finite]
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
    # the line -- and because the fit reports the z=0 crossing, a meter or
    # more behind the observations, a small angular error is levered into a
    # large baseline error. Dive 103's 6.25 cm baseline came from 17
    # otherwise clean observations.
    fitted_points = trim_outlying_observations(observations)
    trimmed = count - len(fitted_points)
    kept = _kept(observations, fitted_points)
    # `calibrate_laser` casts to the kernel's float32 itself.
    origin, orientation = _calibrate_laser(fitted_points)
    # The Rust kernel returns the origin with z=0 implicit; pad to a 3-vector.
    laser_position = np.array([float(origin[0]), float(origin[1]), 0.0])
    laser_axis = np.asarray(orientation, dtype=float)
    matrix = np.asarray(camera_matrix, dtype=float)

    # The dots are read only by the gates that need them, so a malformed
    # payload cannot pre-empt an earlier gate's recorded refusal. The fit's
    # own dots are those of the points it fitted.
    def fit_dots() -> np.ndarray:
        return np.asarray(dots, dtype=float).reshape(-1, 2)[finite][kept]

    def dive_dots() -> np.ndarray:
        return np.asarray(mission_dots, dtype=float).reshape(-1, 2)

    verdicts = dict(not_run)
    gates = (
        # Underdetermination first: the two projection gates below abstain on
        # exactly the degenerate geometry that most needs refusing.
        (
            "observation_geometry",
            lambda: check_observation_geometry(
                fitted_points, remedies=REFUSAL_REMEDIES
            ),
        ),
        (
            "fit_self_consistency",
            lambda: check_fit_self_consistency(
                laser_position,
                laser_axis,
                matrix,
                fit_dots(),
                remedies=REFUSAL_REMEDIES,
            ),
        ),
        # Not redundant with the above: that compares the ray's projection to
        # the dots and is structurally blind to the laser's offset.
        (
            "baseline_plausible",
            lambda: check_baseline_plausible(
                laser_position,
                min_baseline_m=MIN_BASELINE_M,
                max_baseline_m=MAX_BASELINE_M,
                remedies=REFUSAL_REMEDIES,
            ),
        ),
        # And the question none of the above asks: does this calibration
        # describe the dive it will measure? A mid-dive re-seat leaves the fit
        # consistent with its own burst and wrong for every frame after it.
        (
            "describes_dive",
            lambda: check_calibration_describes_mission(
                laser_position,
                laser_axis,
                matrix,
                dive_dots(),
                remedies=REFUSAL_REMEDIES,
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
