"""What the focal-length backfill returns: the report an operator reads before
applying.

New in v2 (2026-10-25). Every frame read lands in exactly one bucket -- a
focal length, unreadable, or not on the NAS -- so a mission's report accounts
for all of it, and the dry run is the corpus count the zoom gate is proven on.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel

__all__ = [
    "FocalLengthBackfillReport",
    "FocalLengthMissionRef",
    "FocalLengthMissionReport",
]

#: At most this many paths are listed per bucket (the counts are whole): the
#: report sits in workflow history, once per mission.
LISTED = 20


class FocalLengthMissionRef(BaseModel):
    """A mission with frames still to read. Numbers are per tenant, so the
    tenant and mission travel with it."""

    tenant_id: str
    mission_id: str
    mission_number: int
    frames: int


class FocalLengthMissionReport(BaseModel):
    mission_number: int
    #: False: a dry run, which reads and writes nothing.
    applied: bool
    #: Frames read (a NAS path and no focal length when listed).
    frames: int = 0
    #: Frames by focal length, mm to two decimals ("4.50").
    focal_lengths: Dict[str, int] = {}
    #: Frames whose focal length is not the TG-6's calibrated wide end.
    zoomed: int = 0
    #: No FocalLength tag in the header (first `LISTED`).
    unreadable: List[str] = []
    unreadable_count: int = 0
    #: Not at its path on the NAS (first `LISTED`).
    not_found: List[str] = []
    not_found_count: int = 0
    recorded: int = 0
    #: Already had a focal length when written (recorded meanwhile).
    already_known: int = 0


class FocalLengthBackfillReport(BaseModel):
    applied: bool
    missions: List[FocalLengthMissionReport] = []

    @property
    def frames(self) -> int:
        return sum(m.frames for m in self.missions)

    @property
    def zoomed(self) -> int:
        return sum(m.zoomed for m in self.missions)
