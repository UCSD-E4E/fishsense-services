# pylint: disable=protected-access
# The private kernels (`_drop_skipped`, `_laser_point_in_camera_space`) are
# unit-tested directly — they carry the correspondence-pairing invariants that
# a full-activity test can only exercise indirectly.
"""Unit tests for stage 13's fit (the processor's `perform_laser_calibration`).

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_perform_laser_calibration_activity.py. The
end-to-end synthetic-scene test pins down the math on a known laser line;
tolerance matches v1's prod-comparison thresholds (axis < 0.5 deg, position
< 1 mm). Names, scenes and reasons are v1's.

v2 changes, each pinned here:

* **the processor reads and writes nothing.** v1's activity fetched the dive,
  slate, labels and intrinsics through the SDK and PUT the extrinsics; here
  they arrive in a `SlateCalibrationInput` and the activity returns a
  `LaserCalibrationResult`, which the orchestrator persists. So v1's two
  no-op tests (no slate, no slate labels) moved to the orchestrator, which
  now decides that before dispatching;
* **a refusal is returned, not raised.** v1 recorded it through the SDK and
  raised a non-retryable `ApplicationError`; here the result is `refused`
  with v1's error type and message, and the orchestrator records it and
  raises (so the workflow still fails loud, and recording still cannot mask
  the refusal);
* observations are keyed by capture, and each carries its own laser dot: the
  orchestrator resolved v1's `get_laser_label(image_id).first()` (no
  ordering) to the lowest live label, deterministically.
"""

from __future__ import annotations

from uuid import UUID

import numpy as np
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.slate_calibration import (
    LaserCalibrationResult,
    SlateCalibrationInput,
    SlateObservation,
)
from fishsense_services_processor.laser_calibration import activities as sut

CAMERA_MATRIX = np.array(
    [
        [3000.0, 0.0, 2048.0],
        [0.0, 3000.0, 1536.0],
        [0.0, 0.0, 1.0],
    ]
)
DIVE = UUID(int=42)
DPI = 300
TEMPLATE = [
    (0.0, 0.0),
    (2400.0, 0.0),
    (0.0, 3000.0),
    (2400.0, 3000.0),
    (1200.0, 0.0),
    (1200.0, 3000.0),
]


def _project(point_camera: np.ndarray) -> tuple[float, float]:
    p = CAMERA_MATRIX @ point_camera
    return float(p[0] / p[2]), float(p[1] / p[2])


def _payload(observations, dive_dots=None) -> SlateCalibrationInput:
    """The dive's own dots default to the calibration frames' dots, the
    honest stand-in for "the whole dive agrees" (v1's fixture did the same)."""
    if dive_dots is None:
        dive_dots = [(o.laser_x, o.laser_y) for o in observations]
    return SlateCalibrationInput(
        dive_id=DIVE,
        camera_matrix=CAMERA_MATRIX.tolist(),
        template_points=TEMPLATE,
        dpi=DPI,
        observations=observations,
        dive_dots=dive_dots,
    )


def _build_synthetic_scene(
    n_observations: int,
    laser_origin_world: np.ndarray,
    laser_axis_world: np.ndarray,
    slate_distances: list[float],
) -> list[SlateObservation]:
    """Render n synthetic slate observations.

    Each observation places the slate at a different depth along +Z
    with a small lateral offset so the PnP poses differ. The laser's
    intersection with that plane is projected to give the laser pixel.
    """
    src_pts = np.array(TEMPLATE)

    body = np.zeros((len(src_pts), 3), dtype=np.float64)
    body[:, :2] = (src_pts / float(DPI)) * sut.INCH_TO_M

    observations: list[SlateObservation] = []
    for i in range(n_observations):
        depth = slate_distances[i]
        camera_space = body.copy()
        camera_space[:, 2] = depth
        centroid_xy = body[:, :2].mean(axis=0)
        camera_space[:, 0] -= centroid_xy[0]
        camera_space[:, 1] -= centroid_xy[1]
        camera_space[:, 0] += 0.02 * (i - n_observations / 2)

        ref_pixels = [_project(p) for p in camera_space]

        # Plane normal +Z, plane offset = depth.
        t = (depth - laser_origin_world[2]) / laser_axis_world[2]
        laser_world = laser_origin_world + t * laser_axis_world
        laser_pixel = _project(laser_world)

        observations.append(
            SlateObservation(
                capture_id=UUID(int=100 + i),
                reference_points=ref_pixels,
                skipped_points=None,
                laser_x=laser_pixel[0],
                laser_y=laser_pixel[1],
            )
        )
    return observations


async def _run(payload) -> LaserCalibrationResult:
    return await ActivityEnvironment().run(sut.perform_laser_calibration, payload)


async def test_refuses_when_too_few_usable_laser_points():
    observations = _build_synthetic_scene(
        n_observations=1,
        laser_origin_world=np.array([-0.03, -0.10, 0.0]),
        laser_axis_world=np.array([0.0, 0.0, 1.0]),
        slate_distances=[0.5],
    )

    result = await _run(_payload(observations))

    assert result.outcome == "refused"
    assert result.refusal_type == "InsufficientLaserPoints"
    assert "insufficient laser points" in result.refusal_reason
    assert result.laser_position is None


async def test_recovers_known_laser_extrinsics_from_synthetic_scene():
    laser_origin = np.array([-0.03, -0.10, 0.0])
    laser_axis = np.array([0.005, -0.02, 1.0])
    laser_axis = laser_axis / np.linalg.norm(laser_axis)

    observations = _build_synthetic_scene(
        n_observations=6,
        laser_origin_world=laser_origin,
        laser_axis_world=laser_axis,
        slate_distances=[0.40, 0.55, 0.70, 0.85, 1.00, 1.15],
    )

    result = await _run(_payload(observations))

    assert result.outcome == "accepted"
    assert result.observation_count == 6

    fitted_axis = np.asarray(result.laser_axis, dtype=float)
    fitted_axis = fitted_axis / np.linalg.norm(fitted_axis)
    cos = float(np.clip(np.dot(fitted_axis, laser_axis), -1.0, 1.0))
    angle_deg = float(np.degrees(np.arccos(abs(cos))))
    assert angle_deg < 0.5

    fitted_pos = np.asarray(result.laser_position, dtype=float)
    pos_xy_l2 = float(np.linalg.norm(fitted_pos[:2] - laser_origin[:2]))
    assert pos_xy_l2 < 0.001


async def test_reflection_contaminated_scene_is_rejected_not_persisted():
    """The prod-dive-77 regression: half the laser dots mislabeled onto a
    parallel artifact line (specular reflection on the pool slate) produce a
    fit that reprojects onto NEITHER dot population. The self-consistency
    gate must refuse — the old behavior shipped the broken calibration, and a
    borrowing fish-model dive measured +31..+137% length errors."""
    from fishsense_services_processor.calibration.consistency import (
        CalibrationInconsistentError,
    )

    laser_origin = np.array([-0.03, -0.10, 0.0])
    laser_axis = np.array([0.005, -0.02, 1.0])
    laser_axis = laser_axis / np.linalg.norm(laser_axis)

    observations = _build_synthetic_scene(
        n_observations=8,
        laser_origin_world=laser_origin,
        laser_axis_world=laser_axis,
        slate_distances=[0.40, 0.55, 0.70, 0.85, 1.00, 1.15, 1.30, 1.45],
    )
    # Contaminate: shift half the laser pixels +45px in x — the reflection
    # artifact observed on prod dive 77 (labelers clicked the specular double).
    for i, observation in enumerate(observations):
        if i % 2 == 0:
            observation.laser_x = float(observation.laser_x) + 45.0

    result = await _run(_payload(observations))

    # The original type is preserved so the record still says which gate
    # fired (v1 raised it as the ApplicationError's type).
    assert result.outcome == "refused"
    assert result.refusal_type == CalibrationInconsistentError.__name__
    assert result.gate_verdicts["fit_self_consistency"] == "refused"


# --------------------------- skipped-points (Bug 2) ---------------------------


def test_drop_skipped_resolves_indices_against_original_list():
    """skipped=[2,5] on 8 points keeps {0,1,3,4,6,7} — NOT the {0,1,3,4,5,7}
    a sequential pop() would leave. Regression for the stage-13 mis-pair."""
    pts = list("abcdefgh")
    assert sut._drop_skipped(pts, [2, 5]) == ["a", "b", "d", "e", "g", "h"]
    # single skip (the only case the old pop() got right) still works
    assert sut._drop_skipped(pts, [5]) == ["a", "b", "c", "d", "e", "g", "h"]
    # no skip is identity
    assert sut._drop_skipped(pts, []) == pts


def test_drop_skipped_rejects_duplicate_and_out_of_range():
    import pytest

    with pytest.raises(ValueError):
        sut._drop_skipped(list("abcdef"), [2, 2])
    with pytest.raises(ValueError):
        sut._drop_skipped(list("abcdef"), [6])
    with pytest.raises(ValueError):
        sut._drop_skipped(list("abcdef"), [-1])


def _observation(reference_points, skipped_points=None) -> SlateObservation:
    return SlateObservation(
        capture_id=UUID(int=1),
        reference_points=reference_points,
        skipped_points=skipped_points,
        laser_x=100.0,
        laser_y=100.0,
    )


def test_laser_point_skips_when_reference_count_mismatches_template():
    """A labeled point count that disagrees with (template - skipped) must not
    mis-pair solvePnP correspondences -- but it must cost only that *label*.

    This used to raise, which escaped `_gather_laser_points`' `is not None`
    skip and failed the whole dive. Prod dive 526 carries 17 completed slate
    labels of which 2 hold a JSON `null` for `reference_points`; those 2
    discarded the 15 good observations and the dive never calibrated. Worse,
    the bare `ValueError` bypassed the refusal record, so the dive stayed in
    the cohort and blocked it -- 10 consecutive hourly failures on 2026-09-12.

    Returning None keeps the pairing guarantee (nothing mis-paired) and routes
    the starved case into the existing `MIN_LASER_POINTS` refusal, which is
    recorded. See `test_mismatched_labels_are_skipped_not_fatal` for the
    dive-level behaviour.
    """
    # 6 template - 1 skipped = 5 expected, but only 4 provided
    observation = _observation(
        [(10.0, 10.0), (20.0, 10.0), (10.0, 20.0), (20.0, 20.0)], skipped_points=[2]
    )
    assert (
        sut._laser_point_in_camera_space(observation, TEMPLATE, DPI, CAMERA_MATRIX)
        is None
    )


def test_laser_point_skips_label_with_null_reference_points():
    """The prod dive 526 shape: `reference_points` is a JSON `null`, so the
    label contributes 0 points against a 6-point template."""
    assert (
        sut._laser_point_in_camera_space(
            _observation(None), TEMPLATE, DPI, CAMERA_MATRIX
        )
        is None
    )


async def test_mismatched_labels_are_skipped_not_fatal():
    """Dive 526 regression: 2 of 8 labels carry a JSON `null` for
    `reference_points`, and the remaining 6 still recover the known extrinsics.

    Before the fix this raised out of `_gather_laser_points` and failed the
    dive, discarding 6 perfectly good observations.
    """
    laser_origin = np.array([-0.03, -0.10, 0.0])
    laser_axis = np.array([0.005, -0.02, 1.0])
    laser_axis = laser_axis / np.linalg.norm(laser_axis)

    observations = _build_synthetic_scene(
        n_observations=8,
        laser_origin_world=laser_origin,
        laser_axis_world=laser_axis,
        slate_distances=[0.40, 0.55, 0.70, 0.85, 1.00, 1.15, 1.30, 1.45],
    )
    # The two 526-shaped labels. Their laser dots stay perfectly good, which
    # is the point: the dot is fine, only the slate corners are missing.
    observations[2].reference_points = None
    observations[5].reference_points = None

    result = await _run(_payload(observations))

    assert result.outcome == "accepted"
    assert result.observation_count == 6

    fitted_axis = np.asarray(result.laser_axis, dtype=float)
    fitted_axis = fitted_axis / np.linalg.norm(fitted_axis)
    cos = float(np.clip(np.dot(fitted_axis, laser_axis), -1.0, 1.0))
    assert float(np.degrees(np.arccos(abs(cos)))) < 0.5

    fitted_pos = np.asarray(result.laser_position, dtype=float)
    assert float(np.linalg.norm(fitted_pos[:2] - laser_origin[:2])) < 0.001


async def test_mismatched_labels_that_starve_the_dive_record_a_refusal():
    """Skipping must not silently wedge the cohort either.

    When the surviving observations fall below `MIN_LASER_POINTS`, the existing
    refusal path is what fires -- and unlike the old bare `ValueError`, it
    comes back as a refusal the orchestrator records, so the dive leaves the
    cohort.
    """
    observations = _build_synthetic_scene(
        n_observations=3,
        laser_origin_world=np.array([-0.03, -0.10, 0.0]),
        laser_axis_world=np.array([0.0, 0.0, 1.0]),
        slate_distances=[0.5, 0.7, 0.9],
    )
    observations[1].reference_points = None
    observations[2].reference_points = None

    result = await _run(_payload(observations))

    assert result.outcome == "refused"
    assert "insufficient laser points" in result.refusal_reason
    assert result.observation_count == 1


async def test_a_successful_fit_says_how_it_was_judged():
    """v2: the row records each gate's verdict, the lever arm, the count and
    fishsense-core's version (PLAN.md §4.3), so a stored calibration can be
    audited without re-running it."""
    import importlib.metadata

    observations = _build_synthetic_scene(
        n_observations=6,
        laser_origin_world=np.array([-0.03, -0.10, 0.0]),
        laser_axis_world=np.array([0.005, -0.02, 1.0])
        / np.linalg.norm([0.005, -0.02, 1.0]),
        slate_distances=[0.40, 0.55, 0.70, 0.85, 1.00, 1.15],
    )

    result = await _run(_payload(observations))

    assert result.gate_verdicts == {
        "observation_geometry": "passed",
        "fit_self_consistency": "passed",
        "baseline_plausible": "passed",
        # six dots is the gate's floor, but they span too little to judge
        "describes_dive": result.gate_verdicts["describes_dive"],
    }
    assert result.gate_verdicts["describes_dive"] in {"passed", "abstained"}
    assert result.lever_arm_m is not None
    assert abs(result.lever_arm_m - 0.75) < 0.01
    assert result.core_version == importlib.metadata.version("fishsense-core")
