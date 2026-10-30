"""The checked borrow's payloads (v27): the borrower's posed slate points and
the one beam it would borrow, and the check's verdict."""

import uuid

from fishsense_services_contracts.calibration_borrow import (
    BorrowCheckInput,
    BorrowCheckReport,
)


def test_a_check_request_carries_the_posed_points_and_the_lenders_beam():
    m, lender = uuid.uuid4(), uuid.uuid4()
    r = BorrowCheckInput(mission_id=m, lender_id=lender,
                         laser_position=[0.0, 0.0, 0.0], laser_axis=[0.0, 0.0, 1.0],
                         pose_points={"a": [0.1, 0.0, 1.0], "b": None})  # fmt: skip
    wire = r.model_dump(mode="json", by_alias=True)
    back = BorrowCheckInput.model_validate(wire)
    assert (back.mission_id, back.lender_id) == (m, lender)
    assert back.pose_points["b"] is None


def test_a_check_says_how_far_the_beam_reads_the_slates_depth_and_on_what():
    r = BorrowCheckReport(mission_id=uuid.uuid4(), lender_id=uuid.uuid4(),
                          outcome="refused", refusal_reason="slates_disagree",
                          depth_ratio=0.07, holds=3, points=5,
                          capture_ids=[uuid.uuid4()])  # fmt: skip
    back = BorrowCheckReport.model_validate(r.model_dump(mode="json"))
    assert (back.depth_ratio, back.holds, back.points) == (0.07, 3, 5)
    assert BorrowCheckReport(mission_id=uuid.uuid4(), lender_id=uuid.uuid4(),
                             outcome="refused",
                             refusal_reason="too_few_posed").depth_ratio is None  # fmt: skip
