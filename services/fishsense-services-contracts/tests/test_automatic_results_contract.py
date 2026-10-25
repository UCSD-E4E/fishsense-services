"""The automatic-results contract (new in v2): the gate, the statuses, the box."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
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
        # 0044 (review of #85): a kept mask with no nameable score, and a
        # frame left out of three runs.
        "mask_unscored", "frame_failed",
    }  # fmt: skip


def test_a_mask_box_must_be_a_box():
    with pytest.raises(ValidationError):
        AutomaticFrameResult(
            capture_id=uuid4(), status="predicted", predictor_version=1,
            mask_bbox=[10, 10, 5, 20],
        )  # fmt: skip


def test_a_frame_result_may_carry_the_dots_colour():
    """v21 (2026-10-24): the automatic dot's colour, read off its pixels by
    the laser stage's kernel, as the human path's prediction carries it."""
    result = AutomaticFrameResult(
        capture_id=uuid4(),
        status="no_fish_at_dot",
        predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        laser_color="green",
        laser_color_margin=0.31,
    )

    assert (result.laser_color, result.laser_color_margin) == ("green", 0.31)


def test_a_colour_is_red_or_green():
    with pytest.raises(ValidationError):
        AutomaticFrameResult(
            capture_id=uuid4(),
            status="no_fish_at_dot",
            predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
            laser_color="blue",
        )


def test_a_v20_frame_result_still_parses_with_no_colour():
    """A processor still on v20 sends no colour; the orchestrator takes it."""
    result = AutomaticFrameResult.model_validate(
        {
            "capture_id": str(uuid4()),
            "status": "no_laser_dot",
            "predictor_version": AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        }
    )

    assert (result.laser_color, result.laser_color_margin) == (None, None)
