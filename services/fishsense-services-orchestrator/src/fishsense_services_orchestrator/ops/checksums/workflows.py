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
        DiveVerificationSummary,
        VerifyAllDivesProgress,
        VerifyAllDivesReport,
        VerifyChecksumsReport,
    )

__all__ = ["VerifyAllDivesChecksumsWorkflow", "VerifyDiveChecksumsWorkflow"]

# One dive can be ~500 whole-file downloads driven serially.
_PER_DIVE_TIMEOUT = timedelta(hours=6)
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


async def _verify(dive_number: int, limit: Optional[int]) -> VerifyChecksumsReport:
    return await workflow.execute_activity(
        "verify_dive_checksums",
        args=(dive_number, limit),
        # Without it the payload decodes to a plain dict.
        result_type=VerifyChecksumsReport,
        schedule_to_close_timeout=_PER_DIVE_TIMEOUT,
        heartbeat_timeout=_HEARTBEAT_TIMEOUT,
        retry_policy=_VERIFY_RETRY,
    )


@workflow.defn
class VerifyDiveChecksumsWorkflow:
    # pylint: disable=too-few-public-methods
    """Compare one dive's stored checksums and capture times with the NAS."""

    @workflow.run
    async def run(
        self, dive_number: int, limit: Optional[int] = None
    ) -> VerifyChecksumsReport:
        """Verify the dive, optionally sampling its first `limit` frames."""
        return await _verify(dive_number, limit)


@workflow.defn
class VerifyAllDivesChecksumsWorkflow:
    """Verify every dive with a canonical capture, one at a time."""

    def __init__(self) -> None:
        self._progress = VerifyAllDivesProgress()

    @workflow.query
    def progress(self) -> VerifyAllDivesProgress:
        """Live counts: a full sweep runs for days."""
        return self._progress

    @workflow.run
    async def run(
        self,
        limit_per_dive: Optional[int] = None,
        dive_numbers: Optional[List[int]] = None,
    ) -> VerifyAllDivesReport:
        if dive_numbers is None:
            self._progress.state = "selecting"
            dive_numbers = await workflow.execute_activity(
                "select_canonical_dive_numbers",
                result_type=List[int],
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, non_retryable_error_types=["NotAMember"]
                ),
            )

        report = VerifyAllDivesReport(dives_requested=len(dive_numbers))
        self._progress.state = "verifying"
        self._progress.total_dives = len(dive_numbers)

        for dive_number in dive_numbers:
            self._progress.current_dive_number = dive_number
            try:
                result = await _verify(dive_number, limit_per_dive)
                summary = DiveVerificationSummary(
                    dive_number=result.dive_number,
                    checked=result.checked,
                    total_in_dive=result.total_in_dive,
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
                    "verification failed dive=%d: %s", dive_number, exc
                )
                report.dives.append(
                    DiveVerificationSummary(dive_number=dive_number, error=str(exc))
                )
                self._progress.dives_errored += 1
                self._progress.dives_with_findings += 1
                self._progress.dives_done += 1
                continue

            report.dives.append(summary)
            report.dives_verified += 1
            report.images_checked += summary.checked
            report.checksum_matched += summary.checksum_matched

            self._progress.dives_done += 1
            self._progress.images_checked += summary.checked
            self._progress.checksum_matched += summary.checksum_matched
            if not summary.is_clean:
                self._progress.dives_with_findings += 1

        self._progress.state = "done"
        self._progress.current_dive_number = None
        workflow.logger.info(
            "sweep complete dives=%d verified=%d images=%d matched=%d findings=%d",
            report.dives_requested,
            report.dives_verified,
            report.images_checked,
            report.checksum_matched,
            len(report.dives_with_findings),
        )
        return report
