"""The checked borrow (v27, 2026-10-29): an aim with no calibration of its
own may take a neighbor's beam, checked on the borrower's own slates. The API
picks the beam (the same device, the same day, along the aim's dot line);
the borrower's slate frames are posed on the GPU (`calibration_v4`); the
processor reads each posed point's depth off that beam and compares it with
the pose's. New models only; v26 parses."""

from __future__ import annotations

from typing import Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel

__all__ = ["BorrowCheckInput", "BorrowCheckReport"]


class BorrowCheckInput(BaseModel):
    """The borrower's posed slate points (by capture id, camera-space m; None:
    not posed) and the beam it would borrow (`lender_id`'s, camera-space)."""

    mission_id: UUID
    lender_id: UUID
    laser_position: List[float]
    laser_axis: List[float]
    pose_points: Dict[str, Optional[List[float]]] = {}


class BorrowCheckReport(BaseModel):
    """`depth_ratio`: the median over slate holds of each hold's median
    (beam depth / pose depth - 1); `holds` and `points` it rests on, and the
    frames those points are (`capture_ids`)."""

    mission_id: UUID
    lender_id: UUID
    outcome: str
    refusal_reason: Optional[str] = None
    depth_ratio: Optional[float] = None
    holds: int = 0
    points: int = 0
    capture_ids: List[UUID] = []
