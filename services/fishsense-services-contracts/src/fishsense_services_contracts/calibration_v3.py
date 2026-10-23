"""Label-free calibration v3's replay (v15, 2026-10-22): what v3 would make of
a mission, read-only, and of random draws of its slate frames (the small-n
control). New models only; production's calibration is v2 until the owner
switches it."""

from __future__ import annotations

from typing import Annotated, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fishsense_services_contracts.automatic_results import FitAutomaticCalibrationInput
from fishsense_services_contracts.renamed import renamed_from

__all__ = ["CalibrationDraw", "CalibrationV3Report", "ReplayCalibrationInput"]


class CalibrationDraw(BaseModel):
    """v3 on `size` slate frames drawn at random (`seed`) from the mission's."""

    size: int
    seed: int
    outcome: str
    refusal_reason: Optional[str] = None
    laser_position: Optional[List[float]] = None
    laser_axis: Optional[List[float]] = None
    vanishing_px: Optional[float] = None
    frames_used: int = 0
    check_median_px: Optional[float] = None
    check_within_share: Optional[float] = None


class CalibrationV3Report(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    outcome: str
    refusal_reason: Optional[str] = None
    laser_position: Optional[List[float]] = None
    laser_axis: Optional[List[float]] = None
    vanishing_px: Optional[float] = None
    line_direction: Optional[List[float]] = None
    line_offset_px: Optional[float] = None
    candidate_count: int = 0
    frames_used: int = 0
    pair_count: int = 0
    size_ratio: Optional[float] = None
    se_px: Optional[float] = None
    gross_dropped: int = 0
    consensus_dropped: int = 0
    rival: int = 0
    check_dots: int = 0
    check_median_px: Optional[float] = None
    check_within_share: Optional[float] = None
    draws: List[CalibrationDraw] = []


class ReplayCalibrationInput(BaseModel):
    payload: FitAutomaticCalibrationInput
    #: Slate-frame counts to draw, and how many draws of each (0: none).
    draw_sizes: List[int] = []
    draws_per_size: int = 0
