"""The checked borrow's check (v27; the owner, 2026-10-29): a neighbor's beam
reads each of the borrower's posed slate points' depth, against the pose's.

Its control has a known answer: points cast by a known beam, so the true beam
reads them at ratio 0 and a beam tilted to read 8% deep is refused."""

from __future__ import annotations

import uuid

import numpy as np
import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.calibration_borrow import BorrowCheckInput
from fishsense_services_processor.automatic_calibration import borrow
from fishsense_services_processor.automatic_calibration.activities import (
    AutomaticCalibrationActivities,
)

O = [-0.0325, -0.0997, 0.0]
A = [0.031, 0.030, 0.999]


def _on(beam_o, beam_a, z):
    o, a = np.asarray(beam_o), np.asarray(beam_a) / np.linalg.norm(beam_a)
    return list(o + ((z - o[2]) / a[2]) * a)


def _points(depths, *, o=O, a=A):
    """Two frames per hold (5 mm apart), a hold at each depth."""
    out = {}
    for z in depths:
        for dz in (0.0, 0.005):
            out[str(uuid.uuid4())] = _on(o, a, z + dz)
    return out


def _request(points, axis=A):
    return BorrowCheckInput(mission_id=uuid.uuid4(), lender_id=uuid.uuid4(),
                            laser_position=O,
                            laser_axis=axis, pose_points=points)  # fmt: skip


def _deeper(ratio, z=1.5):
    """An axis from `O` that reads a dot at range `z` on `A` `ratio` deep:
    through the point on that dot's camera ray at (1 + ratio) z."""
    p = np.asarray(_on(O, A, z))
    target = p * (1 + ratio)
    a = target - np.asarray(O)
    return list(a / np.linalg.norm(a))


def test_the_beam_that_cast_the_points_reads_them_at_their_depth():
    report = borrow.check_borrowed_beam(_request(_points([0.8, 1.4, 2.2])))
    assert report.outcome == "accepted"
    assert abs(report.depth_ratio) < 1e-6
    assert (report.holds, report.points, len(report.capture_ids)) == (3, 6, 6)


def test_a_beam_that_reads_the_slates_8_percent_deep_is_refused():
    report = borrow.check_borrowed_beam(
        _request(_points([0.8, 1.4, 2.2]), axis=_deeper(0.08))
    )
    assert report.outcome == "refused" and report.refusal_reason == "slates_disagree"
    assert report.depth_ratio > borrow.DEPTH_TOLERANCE


def test_a_beam_within_the_tolerance_is_borrowed():
    report = borrow.check_borrowed_beam(
        _request(_points([0.8, 1.4, 2.2]), axis=_deeper(0.03))
    )
    assert report.outcome == "accepted"
    assert 0.0 < report.depth_ratio < borrow.DEPTH_TOLERANCE


def test_the_ratio_is_a_median_over_holds_so_one_bad_hold_does_not_decide():
    points = _points([0.8, 1.4, 2.2])
    # A third hold's two frames read 30% off (a misposed slate).
    for key in list(points)[4:]:
        points[key] = list(np.asarray(points[key]) * 1.3)
    report = borrow.check_borrowed_beam(_request(points))
    assert report.outcome == "accepted" and abs(report.depth_ratio) < 0.02


@pytest.mark.parametrize("points", [{}, {"a": None, "b": [0.0, 0.0, 1.0]}])
def test_fewer_than_two_posed_points_are_no_check(points):
    report = borrow.check_borrowed_beam(_request(points))
    assert report.outcome == "refused" and report.refusal_reason == "too_few_posed"
    assert report.depth_ratio is None


def test_an_unposed_frame_is_not_read():
    points = {**_points([0.8, 1.4]), "unposed": None}
    report = borrow.check_borrowed_beam(_request(points))
    assert report.points == 4 and "unposed" not in map(str, report.capture_ids)


async def test_the_activity_checks_without_reading_an_image():
    def no_store():
        raise AssertionError("no image is read for the check")

    acts = AutomaticCalibrationActivities(store_factory=no_store)
    report = await ActivityEnvironment().run(
        acts.check_borrowed_beam, _request(_points([0.8, 1.4]))
    )
    assert report.outcome == "accepted"
