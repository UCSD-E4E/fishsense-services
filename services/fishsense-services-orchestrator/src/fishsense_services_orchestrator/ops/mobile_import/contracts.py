"""What an FSM import is asked to do, and the report an operator reads.

New in v2 (2026-10-21; the owner: CCFRP's summer 2025 FishSense Mobile data
into ccfrp-ucsd). A dry run unless ``apply``; an apply names the plan it
reviewed (``plan_sha256``).
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List

from pydantic import BaseModel, Field

__all__ = ["FsmImportReport", "FsmImportRequest"]


class FsmImportRequest(BaseModel):
    #: The dataset folder on the NAS: ``<dataset>/ED-xx/<phone>/FSM``.
    dataset: str
    #: The tenant's slug; the orchestrator must be one of its members.
    tenant: str
    #: The window, inclusive, by the local date in ``time_zone``.
    captured_from: date
    captured_to: date
    #: The IANA zone the phones were in: the window's dates, and each
    #: mission's day (0057).
    time_zone: str
    #: Row-major K per phone folder name (``iPad``, ``iPhone``). The export
    #: has none, so they come from elsewhere, named in ``intrinsics_source``.
    intrinsics: Dict[str, List[float]]
    intrinsics_source: str = Field(min_length=1)


class FsmImportReport(BaseModel):
    #: False: a dry run, which wrote nothing.
    applied: bool
    #: Pass this back to apply this plan, and only this plan.
    plan_sha256: str
    #: Captures the plan holds: a database row whose photo is in its folder,
    #: taken within the window, its depth the size it says.
    planned: int
    by_day: Dict[str, int]
    by_phone: Dict[str, int]
    by_folder: Dict[str, int]
    #: Rows left out, by why: ``outside_window``, ``no_photo`` (no folder
    #: holds its photo), ``depth_size_mismatch``.
    excluded: Dict[str, int]
    #: Apply only: imported now, and already there (a retry, or a second run).
    imported: int = 0
    already_imported: int = 0
