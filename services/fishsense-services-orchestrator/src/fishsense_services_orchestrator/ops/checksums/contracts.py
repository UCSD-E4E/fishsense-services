"""What checksum verification returns: the reports an operator reads.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
ingest_contracts.py (`ChecksumMismatch`, `VerifyChecksumsReport`,
`MissionVerificationSummary`, `VerifyAllMissionsProgress`, `VerifyAllMissionsReport`).
Semantics are v1's. v1 kept them in fishsense-shared because the API and the
api-worker both read them; v2's are started and read only through the
orchestrator (and the Temporal UI), so they live here, not in the processing
contract.

v2 change: a dive and a capture are named by their ``number``
(``dive_number``, ``capture_number``), which is v1's dive and image id for a
migrated row -- the same integers v1's reports carried, under honest names.
"""

from __future__ import annotations

from typing import List, Annotated

from pydantic import BaseModel, ConfigDict
from fishsense_services_contracts.renamed import renamed_from

__all__ = [
    "ChecksumMismatch",
    "MissionVerificationSummary",
    "VerifyAllMissionsProgress",
    "VerifyAllMissionsReport",
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

    model_config = ConfigDict(validate_by_name=True)

    mission_number: Annotated[int, renamed_from("dive_number", "mission_number")]
    #: Captures in the dive, before any sampling limit.
    total_in_mission: Annotated[
        int, renamed_from("total_in_dive", "total_in_mission")
    ] = 0
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


class MissionVerificationSummary(BaseModel):
    """One dive's result in a sweep: counts, plus the findings themselves."""

    model_config = ConfigDict(validate_by_name=True)

    mission_number: Annotated[int, renamed_from("dive_number", "mission_number")]
    checked: int = 0
    total_in_mission: Annotated[
        int, renamed_from("total_in_dive", "total_in_mission")
    ] = 0
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


class VerifyAllMissionsProgress(BaseModel):
    """The sweep's `progress` query. A full sweep is ~930 GB off the NAS, so it
    runs for days and has to be observable while it runs."""

    model_config = ConfigDict(validate_by_name=True)

    state: str = "starting"
    total_missions: Annotated[int, renamed_from("total_dives", "total_missions")] = 0
    missions_done: Annotated[int, renamed_from("dives_done", "missions_done")] = 0
    current_mission_number: Annotated[
        int | None, renamed_from("current_dive_number", "current_mission_number")
    ] = None
    images_checked: int = 0
    checksum_matched: int = 0
    missions_with_findings: Annotated[
        int, renamed_from("dives_with_findings", "missions_with_findings")
    ] = 0
    missions_errored: Annotated[
        int, renamed_from("dives_errored", "missions_errored")
    ] = 0


class VerifyAllMissionsReport(BaseModel):
    """Aggregate across every dive the sweep visited."""

    model_config = ConfigDict(validate_by_name=True)

    missions_requested: Annotated[
        int, renamed_from("dives_requested", "missions_requested")
    ] = 0
    missions_verified: Annotated[
        int, renamed_from("dives_verified", "missions_verified")
    ] = 0
    images_checked: int = 0
    checksum_matched: int = 0
    #: Every dive visited, clean ones included: dropping them would make
    #: "verified, fine" indistinguishable from "never reached".
    missions: Annotated[
        List[MissionVerificationSummary], renamed_from("dives", "missions")
    ] = []

    @property
    def missions_with_findings(self) -> List[MissionVerificationSummary]:
        return [d for d in self.missions if not d.is_clean]
