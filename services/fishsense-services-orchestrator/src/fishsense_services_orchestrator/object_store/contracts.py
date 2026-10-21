"""What the object-store activities take and return.

Ported from fishsense-lite@77e8f8e5: `StageRawBytesResult` and
`CleanupRawBytesResult` (the staging and cleanup activities' summaries). v2
change: the activities take a (tenant, dive) pair, not v1's integer dive id.

These stay inside the orchestrator -- the parent workflows that stage and
clean up are the orchestrator's -- so they are not part of the processing
contract. Any stage's own target with ``tenant_id`` and ``mission_id`` fields
serialises to the same payload as a `StagingTarget`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from fishsense_services_contracts.renamed import renamed_from
from typing import Annotated

__all__ = [
    "CleanupRawBytesResult",
    "ProcessedJpegRequest",
    "StageRawBytesResult",
    "StagingTarget",
]


@dataclass(frozen=True)
class StagingTarget:
    tenant_id: uuid.UUID
    mission_id: Annotated[uuid.UUID, renamed_from("dive_id", "mission_id")]


@dataclass
class StageRawBytesResult:
    """Per-dive staging summary, so the parent can log counts."""

    staged: int  # newly downloaded and uploaded
    skipped_already_present: int
    no_path: int  # frames with no NAS path (or checksum): counted, not staged
    #: Of `staged`, those copied from the tenant's bucket, not the NAS
    #: (2026-10-21).
    from_bucket: int = 0


@dataclass
class CleanupRawBytesResult:
    """Per-dive cleanup summary."""

    deleted: int  # scratch objects deleted from Garage


@dataclass(frozen=True)
class ProcessedJpegRequest:
    """Which capture's JPEG, from which stage's folder."""

    tenant_id: uuid.UUID
    capture_id: uuid.UUID
    folder: str
