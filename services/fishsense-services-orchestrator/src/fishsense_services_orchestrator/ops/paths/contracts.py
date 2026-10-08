"""What path repair returns: the report an operator reads before applying.

New in v2 (2026-10-07). Every frame of the dive lands in exactly one bucket,
so the report accounts for the whole dive: at its path, repaired (or, on a dry
run, would be), or one of the reasons it was left alone.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel

__all__ = ["MovedFrame", "PathFinding", "PathRepairReport"]


class MovedFrame(BaseModel):
    """A frame found in a subfolder, its NAS-computed md5 equal to the row's."""

    capture_number: int
    old_path: str
    new_path: str
    canonical: bool


class PathFinding(BaseModel):
    """A frame not at its path that was left alone, and why."""

    capture_number: int
    path: str
    canonical: bool
    #: Where it might be: none (not found), several (ambiguous), or the one
    #: whose hash disagreed or whose path another capture holds.
    candidates: List[str] = []
    detail: str | None = None


class PathRepairReport(BaseModel):
    dive_number: int
    #: False: a dry run, which changes nothing and fills `would_repair`.
    applied: bool
    #: Captures of the dive with a NAS path.
    frames: int = 0
    at_path: int = 0
    repaired: List[MovedFrame] = []
    would_repair: List[MovedFrame] = []
    #: In no subfolder of its folder (or the folder itself is gone).
    not_found: List[PathFinding] = []
    #: In more than one subfolder: which is the frame is a person's call.
    ambiguous: List[PathFinding] = []
    #: Found, but the NAS's md5 is not the row's: another file of that name.
    checksum_mismatch: List[PathFinding] = []
    #: Found, but another capture already holds that path.
    path_taken: List[PathFinding] = []
    #: Recorded with an algorithm the NAS cannot compute (sha256).
    unsupported: List[PathFinding] = []
    #: Verified, but the row changed before the re-point: left as it is now.
    changed_since_checked: List[PathFinding] = []

    @property
    def left_alone(self) -> int:
        return sum(
            len(bucket)
            for bucket in (
                self.not_found,
                self.ambiguous,
                self.checksum_mismatch,
                self.path_taken,
                self.unsupported,
                self.changed_since_checked,
            )
        )
