"""What checksum verification returns: the reports an operator reads.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
ingest_contracts.py (`ChecksumMismatch`, `VerifyChecksumsReport`,
`DiveVerificationSummary`, `VerifyAllDivesProgress`, `VerifyAllDivesReport`).
Semantics are v1's. v1 kept them in fishsense-shared because the API and the
api-worker both read them; v2's are started and read only through the
orchestrator (and the Temporal UI), so they live here, not in the processing
contract.

v2 change: a dive and a capture are named by their ``number``
(``dive_number``, ``capture_number``), which is v1's dive and image id for a
migrated row -- the same integers v1's reports carried, under honest names.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel

__all__ = [
    "ChecksumMismatch",
    "DiveVerificationSummary",
    "VerifyAllDivesProgress",
    "VerifyAllDivesReport",
    "VerifyChecksumsReport",
]


class ChecksumMismatch(BaseModel):
    """One row whose stored value disagrees with the file on the NAS.

    Both values are carried because "some rows disagree" is not actionable --
    *how* they disagree says whether the migration used a different algorithm
    or the file itself changed.
    """

    capture_number: int | None = None
    path: str
    stored: str | None = None
    computed: str | None = None


class VerifyChecksumsReport(BaseModel):
    """One dive's captures re-hashed against the NAS. Read-only.

    If existing checksums were computed differently, duplicate detection does
    not error, it silently reports zero overlap: a re-ingest of a dive already
    present would look entirely new and every frame would land canonical.
    """

    dive_number: int
    #: Captures in the dive, before any sampling limit.
    total_in_dive: int = 0
    checked: int = 0
    checksum_matched: int = 0
    mismatches: List[ChecksumMismatch] = []
    #: Stored capture time disagreeing with EXIF 0x0132 stamped UTC. Tracked
    #: apart from checksums: a wrong checksum breaks duplicate detection, a
    #: wrong timestamp breaks stage-1 clustering.
    timestamp_mismatches: List[ChecksumMismatch] = []
    #: The row exists but its file does not -- itself one of the answers.
    missing_on_nas: List[ChecksumMismatch] = []
    #: A blank checksum on a row: the column duplicate detection joins on, so
    #: a finding rather than a skip.
    no_stored_checksum: List[ChecksumMismatch] = []


class DiveVerificationSummary(BaseModel):
    """One dive's result in a sweep: counts, plus the findings themselves."""

    dive_number: int
    checked: int = 0
    total_in_dive: int = 0
    checksum_matched: int = 0
    mismatches: List[ChecksumMismatch] = []
    timestamp_mismatches: List[ChecksumMismatch] = []
    missing_on_nas: List[ChecksumMismatch] = []
    no_stored_checksum: List[ChecksumMismatch] = []
    #: Set when the dive could not be verified at all (NAS down, retries
    #: exhausted, no such dive). Distinct from "verified, nothing wrong":
    #: conflating the two would let an unreachable dive read as clean.
    error: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.error is None and not (
            self.mismatches
            or self.timestamp_mismatches
            or self.missing_on_nas
            or self.no_stored_checksum
        )


class VerifyAllDivesProgress(BaseModel):
    """The sweep's `progress` query. A full sweep is ~930 GB off the NAS, so it
    runs for days and has to be observable while it runs."""

    state: str = "starting"
    total_dives: int = 0
    dives_done: int = 0
    current_dive_number: int | None = None
    images_checked: int = 0
    checksum_matched: int = 0
    dives_with_findings: int = 0
    dives_errored: int = 0


class VerifyAllDivesReport(BaseModel):
    """Aggregate across every dive the sweep visited."""

    dives_requested: int = 0
    dives_verified: int = 0
    images_checked: int = 0
    checksum_matched: int = 0
    #: Every dive visited, clean ones included: dropping them would make
    #: "verified, fine" indistinguishable from "never reached".
    dives: List[DiveVerificationSummary] = []

    @property
    def dives_with_findings(self) -> List[DiveVerificationSummary]:
        return [d for d in self.dives if not d.is_clean]
