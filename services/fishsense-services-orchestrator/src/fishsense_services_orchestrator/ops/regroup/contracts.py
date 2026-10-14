"""What a regroup returns: the report an operator reads before applying.

New in v2 (2026-10-14). Numbers are v1's ids for migrated rows (`number`),
so a report reads against the research extracts.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel

__all__ = ["RegroupMove", "RegroupReport"]


class RegroupMove(BaseModel):
    """A measured frame whose fish changes; stage 14 measures it again."""

    capture_number: int
    from_fish: int
    #: None: a new fish, of `to_species`.
    to_fish: int | None
    to_species: str | None
    length_m: float


class RegroupReport(BaseModel):
    dive_number: int
    #: False: a dry run, which writes nothing.
    applied: bool
    #: The dive is already grouped this way; an apply writes nothing.
    noop: bool
    #: Pass this back to apply this plan, and only this plan.
    plan_sha256: str
    priority: str
    clusters_before: int
    clusters_after: int
    #: Current server measurements of real fish on the dive.
    measured_frames: int
    moves: List[RegroupMove] = []
    warnings: List[str] = []
