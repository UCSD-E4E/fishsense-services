"""The laser-depth activity: per capture, the first valid label that
triangulates in front of the camera.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_compute_laser_depths_activity.py. v1's activity did
its own SDK reads and writes, so its tests mocked the SDK; v2's processor only
computes, so the tests about *which* images get visited, which are already
current, and which labels are unusable moved to the store
(fishsense-services-api tests/test_laser_depth_store.py), where the cohort now
decides them. What stays here is the geometry and the per-image choice, with
v1's names, fixtures and reasons.

v2 change, pinned: an image none of whose labels triangulates in front of the
camera comes back **refused, with each label tried**, so the orchestrator can
record it and the cohort stop offering it (v1 counted it and wrote nothing:
the dive-32 wedge, PLAN.md §9.16).
"""

from __future__ import annotations

import importlib.metadata
from uuid import UUID, uuid4

import numpy as np
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    LaserCalibrationGeometry,
    LaserDepthCapture,
    LaserDot,
)
from fishsense_services_processor.laser_depth.activities import compute_laser_depths

CAMERA_MATRIX = np.array(
    [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
)
CALIBRATION_ID = uuid4()
DIVE = uuid4()


def _laser_calibration(axis=(0.0, -0.02, 1.0)) -> LaserCalibrationGeometry:
    axis = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(axis)
    return LaserCalibrationGeometry(
        laser_calibration_id=CALIBRATION_ID,
        laser_position=(-0.03, -0.10, 0.0),
        laser_axis=tuple(axis / norm) if norm else tuple(axis),
    )


def _laser_pixel(calibration: LaserCalibrationGeometry, depth: float):
    """Where the laser dot lands when it hits a plane at `depth`."""
    origin = np.asarray(calibration.laser_position, dtype=float)
    axis = np.asarray(calibration.laser_axis, dtype=float)
    hit = origin + ((depth - origin[2]) / axis[2]) * axis
    projected = CAMERA_MATRIX @ hit
    return float(projected[0] / projected[2]), float(projected[1] / projected[2])


def _dot(label_id: UUID, x, y) -> LaserDot:
    return LaserDot(laser_label_id=label_id, x=x, y=y)


def _input(captures, calibration=None) -> ComputeLaserDepthsInput:
    return ComputeLaserDepthsInput(
        dive_id=DIVE,
        camera_matrix=tuple(tuple(row) for row in CAMERA_MATRIX),
        calibration=calibration or _laser_calibration(),
        captures=captures,
    )


async def _run(payload):
    return await ActivityEnvironment().run(compute_laser_depths, payload)


async def test_records_depth_range_and_provenance():
    calibration = _laser_calibration()
    capture, label = uuid4(), uuid4()
    x, y = _laser_pixel(calibration, 1.20)

    result = await _run(
        _input(
            [LaserDepthCapture(capture_id=capture, laser_labels=[_dot(label, x, y)])]
        )
    )

    (outcome,) = result.captures
    assert outcome.capture_id == capture
    depth = outcome.depth
    assert depth.depth_m == pytest.approx(1.20, abs=1e-3)
    # Off-axis dot, so the slant distance is strictly longer than the depth.
    assert depth.range_m > depth.depth_m
    assert depth.laser_label_id == label
    # The dot was constructed on the laser ray, so the two rays meet and the
    # closest-approach distance collapses to the float32 noise floor.
    assert depth.residual_m == pytest.approx(0.0, abs=1e-5)
    assert outcome.refusals == []


async def test_records_the_residual_for_an_inconsistent_dot():
    """A dot displaced across the laser's epipolar line cannot lie on the
    laser at any depth. The depth is still finite and positive — nothing else
    in the pipeline would question it — and the residual is the only thing
    that says the label and the calibration disagree.

    Stored, not gated: a threshold has to come from the observed distribution,
    and the residual is metric, so the same number means different things at
    0.9 m and 2.5 m."""
    calibration = _laser_calibration()
    x, y = _laser_pixel(calibration, 1.20)

    result = await _run(
        _input(
            [
                LaserDepthCapture(
                    capture_id=uuid4(), laser_labels=[_dot(uuid4(), x, y + 150.0)]
                )
            ]
        )
    )

    depth = result.captures[0].depth
    assert depth.depth_m > 0.0
    assert depth.residual_m > 1e-3


async def test_refuses_to_store_an_impossible_depth():
    """The kernel signals "these rays do not meet" with a zero or negative Z
    rather than NaN, and a negative depth yields a perfectly ordinary-looking
    length downstream (see test_laser_geometry). Storing one would launder a
    bad label into a plausible distance.

    v2: the refusal comes back, naming the label and what it said, so the
    orchestrator records it and the cohort stops offering the image."""
    label = uuid4()
    # Laser offset is -x, so a real dot lands left of the principal point;
    # a label to the right of it inverts the solve.
    result = await _run(
        _input(
            [
                LaserDepthCapture(
                    capture_id=uuid4(), laser_labels=[_dot(label, 3000.0, 1600.0)]
                )
            ]
        )
    )

    (outcome,) = result.captures
    assert outcome.depth is None
    (refusal,) = outcome.refusals
    assert refusal.laser_label_id == label
    assert refusal.reason == "non_positive_depth"
    assert refusal.depth_m <= 0.0
    assert (refusal.x, refusal.y) == (3000.0, 1600.0)


async def test_handles_a_dive_with_no_laser_labels():
    """Nothing to try is not an error."""
    result = await _run(_input([]))

    assert result.captures == []
    assert result.dive_id == DIVE


async def test_duplicate_valid_labels_produce_one_depth_per_image():
    """An image can carry several valid laser labels — 461 prod images do,
    nearly all duplicates of the same dot. Take the first (the orchestrator
    orders them by label number, v1's lowest id) and move on: half the work,
    and the recorded provenance is stable across re-runs instead of flapping
    with iteration order."""
    calibration = _laser_calibration()
    x, y = _laser_pixel(calibration, 1.20)
    first, second = uuid4(), uuid4()

    result = await _run(
        _input(
            [
                LaserDepthCapture(
                    capture_id=uuid4(),
                    laser_labels=[_dot(first, x, y), _dot(second, x, y)],
                )
            ]
        )
    )

    (outcome,) = result.captures
    assert outcome.depth.laser_label_id == first
    assert outcome.refusals == []


async def test_falls_back_to_another_valid_label_when_the_first_is_degenerate():
    """Picking one label per image must not mean giving up on the image.

    Deduplicating to the lowest id made an image whose lowest-id label fails
    the `depth_m > 0` gate produce no depth at all, even with a sibling valid
    label that triangulates fine. Under the image-keyed cohort that image is
    never satisfied, so the dive is offered every hour forever — reintroducing
    the dive-279 wedge from the other direction.
    """
    calibration = _laser_calibration()
    good_x, good_y = _laser_pixel(calibration, 1.20)
    bad, good = uuid4(), uuid4()

    result = await _run(
        _input(
            [
                LaserDepthCapture(
                    capture_id=uuid4(),
                    laser_labels=[
                        # First, but on the wrong side of the principal point
                        # for the laser's offset: the rays only meet behind
                        # the camera.
                        _dot(bad, 30.0, 3000.0),
                        _dot(good, good_x, good_y),
                    ],
                )
            ]
        )
    )

    (outcome,) = result.captures
    assert outcome.depth.laser_label_id == good, "the image has a usable label"
    assert outcome.depth.depth_m > 0.0
    # The dot it was computed at travels back, so the persist can tell it
    # from a dot a labeler moved in the meantime (same label id).
    assert (outcome.depth.x, outcome.depth.y) == (good_x, good_y)
    # v2: the degenerate one is recorded too, so it is not retried for nothing.
    assert [r.laser_label_id for r in outcome.refusals] == [bad]


async def test_counts_the_image_once_when_every_label_is_degenerate():
    """If no label for the image yields a usable geometry, that is one
    unusable image, not one per duplicate label — and each label tried is
    named in its refusal."""
    first, second = uuid4(), uuid4()

    result = await _run(
        _input(
            [
                LaserDepthCapture(
                    capture_id=uuid4(),
                    laser_labels=[
                        _dot(first, 30.0, 3000.0),
                        _dot(second, 31.0, 3001.0),
                    ],
                )
            ]
        )
    )

    (outcome,) = result.captures
    assert outcome.depth is None
    assert [r.laser_label_id for r in outcome.refusals] == [first, second]


async def test_a_depth_that_is_not_a_number_is_refused_as_non_finite():
    """A laser axis parallel to the dot's camera ray has no closest point:
    the kernel answers NaN (fishsense-core's docstring). That is a refusal
    too, and says so, rather than a depth."""
    # The camera ray through the principal point is the optical axis; a laser
    # through the camera centre along it is that same line.
    calibration = LaserCalibrationGeometry(
        laser_calibration_id=CALIBRATION_ID,
        laser_position=(0.0, 0.0, 0.0),
        laser_axis=(0.0, 0.0, 1.0),
    )
    label = uuid4()

    result = await _run(
        _input(
            [
                LaserDepthCapture(
                    capture_id=uuid4(), laser_labels=[_dot(label, 2048.0, 1536.0)]
                )
            ],
            calibration=calibration,
        )
    )

    (outcome,) = result.captures
    assert outcome.depth is None
    (refusal,) = outcome.refusals
    assert refusal.reason == "non_finite_depth"
    assert refusal.depth_m is None


async def test_the_result_names_the_core_that_computed_it():
    """laser_depths requires a core version on every row v2 writes."""
    result = await _run(_input([]))

    assert result.core_version == importlib.metadata.version("fishsense-core")


async def test_a_zero_length_axis_fails_the_activity_for_good():
    """v1: the kernel's ValueError is deliberately not caught — the axis
    belongs to the dive's calibration, so every image is equally
    unprocessable, and failing is the honest blast radius.

    v2: it fails *non-retryably*. v1 retried a deterministic failure for the
    activity's whole hour, holding a light-processor slot for nothing."""
    x, y = 1900.0, 1400.0

    with pytest.raises(ApplicationError) as raised:
        await _run(
            _input(
                [
                    LaserDepthCapture(
                        capture_id=uuid4(), laser_labels=[_dot(uuid4(), x, y)]
                    )
                ],
                calibration=_laser_calibration(axis=(0.0, 0.0, 0.0)),
            )
        )

    assert raised.value.non_retryable
    assert "laser_axis" in str(raised.value)
