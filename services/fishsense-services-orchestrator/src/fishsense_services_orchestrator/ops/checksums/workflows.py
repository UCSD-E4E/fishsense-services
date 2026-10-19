"""Re-hash captures against the NAS: one dive, or a sweep over every dive.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/verify_dive_checksums_workflow.py and
verify_all_dives_checksums_workflow.py. On demand, no schedule, read-only.

They run in the orchestrator, inside the slot, where the NAS credentials live
and on the NAS's own network: ~930 GB of whole-file reads never cross a WAN,
and the reports are durable in Temporal rather than in someone's scrollback.

    # one dive, sampling its first 25 frames (`null` for every frame)
    temporal workflow start --task-queue fishsense_orchestrator \\
        --type VerifyDiveChecksumsWorkflow \\
        --workflow-id verify-checksums-<dive number> \\
        --input <dive number> --input 25

    # the sweep: ~5 frames a dive (~20 GB) answers the migration question;
    # null checks every canonical frame (~930 GB, days)
    temporal workflow start --task-queue fishsense_orchestrator \\
        --type VerifyAllDivesChecksumsWorkflow \\
        --workflow-id verify-sweep-sample --input 5

    # just the dives a sample flagged
    ... --input null --input '[64, 66, 412]'

    temporal workflow query --workflow-id verify-sweep-sample --type progress

v1's rules, kept:

* **dives are verified one at a time.** The sweep is diagnostic and shares one
  NAS with the pipeline's staging; there is no concurrency knob, because the
  right value is 1 and a knob invites raising it;
* **one dive's failure does not abandon the sweep.** It is recorded with an
  ``error``, deliberately a separate field, so an unreachable dive never reads
  as clean. The whole per-dive body sits inside the ``try``: an exception
  escaping a workflow body fails the workflow *task*, which Temporal retries
  forever, so a bug would hang the sweep rather than fail it;
* v1's timeouts and bounded retry (6 h per dive, a 10 min heartbeat, 30 s
  doubling to 5 min, 5 attempts). The NAS client retries internally too.

v2 changes: a dive is named by its number (v1's dive id for a migrated dive),
and the dives span every tenant the orchestrator serves; a lost membership and
an unknown dive are not retried.
"""

from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ops.checksums.contracts import (
        MissionVerificationSummary,
        VerifyAllMissionsProgress,
        VerifyAllMissionsReport,
        VerifyChecksumsReport,
    )

__all__ = ["VerifyAllMissionsChecksumsWorkflow", "VerifyMissionChecksumsWorkflow"]

# One dive can be ~500 whole-file downloads driven serially.
_PER_MISSION_TIMEOUT = timedelta(hours=6)
# One frame at a time, so a gap this long means the transfer is wedged.
_HEARTBEAT_TIMEOUT = timedelta(minutes=10)
# Bounded: this is diagnostic, not load-bearing. If the NAS is having a bad
# day, the answer is to ask again later.
_VERIFY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=["DiveNotFound", "NotAMember"],
)


async def _verify(mission_number: int, limit: Optional[int]) -> VerifyChecksumsReport:
    return await workflow.execute_activity(
        "verify_dive_checksums",
        args=(mission_number, limit),
        # Without it the payload decodes to a plain dict.
        result_type=VerifyChecksumsReport,
        schedule_to_close_timeout=_PER_MISSION_TIMEOUT,
        heartbeat_timeout=_HEARTBEAT_TIMEOUT,
        retry_policy=_VERIFY_RETRY,
    )


@workflow.defn(name="VerifyDiveChecksumsWorkflow")
class VerifyMissionChecksumsWorkflow:
    # pylint: disable=too-few-public-methods
    """Compare one dive's stored checksums and capture times with the NAS."""

    @workflow.run
    async def run(
        self, mission_number: int, limit: Optional[int] = None
    ) -> VerifyChecksumsReport:
        """Verify the dive, optionally sampling its first `limit` frames."""
        return await _verify(mission_number, limit)


@workflow.defn(name="VerifyAllDivesChecksumsWorkflow")
class VerifyAllMissionsChecksumsWorkflow:
    """Verify every dive with a canonical capture, one at a time."""

    def __init__(self) -> None:
        self._progress = VerifyAllMissionsProgress()

    @workflow.query
    def progress(self) -> VerifyAllMissionsProgress:
        """Live counts: a full sweep runs for days."""
        return self._progress

    @workflow.run
    async def run(
        self,
        limit_per_mission: Optional[int] = None,
        mission_numbers: Optional[List[int]] = None,
    ) -> VerifyAllMissionsReport:
        if mission_numbers is None:
            self._progress.state = "selecting"
            mission_numbers = await workflow.execute_activity(
                "select_canonical_dive_numbers",
                result_type=List[int],
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, non_retryable_error_types=["NotAMember"]
                ),
            )

        report = VerifyAllMissionsReport(missions_requested=len(mission_numbers))
        self._progress.state = "verifying"
        self._progress.total_missions = len(mission_numbers)

        for mission_number in mission_numbers:
            self._progress.current_mission_number = mission_number
            try:
                result = await _verify(mission_number, limit_per_mission)
                summary = MissionVerificationSummary(
                    mission_number=result.mission_number,
                    checked=result.checked,
                    total_in_mission=result.total_in_mission,
                    checksum_matched=result.checksum_matched,
                    mismatches=result.mismatches,
                    timestamp_mismatches=result.timestamp_mismatches,
                    missing_on_nas=result.missing_on_nas,
                    no_stored_checksum=result.no_stored_checksum,
                )
            except Exception as exc:  # pylint: disable=broad-except
                # Data about the corpus, not a reason to discard the sweep; and
                # `error` keeps it distinguishable from a clean result.
                workflow.logger.warning(
                    "verification failed dive=%d: %s", mission_number, exc
                )
                report.missions.append(
                    MissionVerificationSummary(
                        mission_number=mission_number, error=str(exc)
                    )
                )
                self._progress.missions_errored += 1
                self._progress.missions_with_findings += 1
                self._progress.missions_done += 1
                continue

            report.missions.append(summary)
            report.missions_verified += 1
            report.images_checked += summary.checked
            report.checksum_matched += summary.checksum_matched

            self._progress.missions_done += 1
            self._progress.images_checked += summary.checked
            self._progress.checksum_matched += summary.checksum_matched
            if not summary.is_clean:
                self._progress.missions_with_findings += 1

        self._progress.state = "done"
        self._progress.current_mission_number = None
        workflow.logger.info(
            "sweep complete dives=%d verified=%d images=%d matched=%d findings=%d",
            report.missions_requested,
            report.missions_verified,
            report.images_checked,
            report.checksum_matched,
            len(report.missions_with_findings),
        )
        return report
