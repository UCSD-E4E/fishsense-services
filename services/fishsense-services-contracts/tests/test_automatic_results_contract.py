"""The automatic-results contract (new in v2): the gate, the statuses, the box."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_STATUSES,
    AUTOMATIC_SAM_SCORE_GATE,
    AutomaticFrameResult,
)


def test_the_sam_gate_is_the_papers():
    """cscw-fishsense2027@96a8da07 PAPER.md §6.3: 0.5, SAM's own confidence."""
    assert AUTOMATIC_SAM_SCORE_GATE == 0.5


def test_every_abstention_is_a_status():
    assert set(AUTOMATIC_HEADTAIL_STATUSES) == {
        "predicted", "no_laser_dot", "no_detections", "laser_off_all_fish",
        "headtail_failed", "decode_failed", "raw_unavailable", "slate_frame",
    }  # fmt: skip


def test_a_mask_box_must_be_a_box():
    with pytest.raises(ValidationError):
        AutomaticFrameResult(
            capture_id=uuid4(), status="predicted", predictor_version=1,
            mask_bbox=[10, 10, 5, 20],
        )  # fmt: skip
