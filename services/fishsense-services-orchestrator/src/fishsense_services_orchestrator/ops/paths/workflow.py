"""Point a dive's moved captures at where their files went: on demand.

New in v2 (2026-10-07). It runs in the orchestrator, in the slot, where the
NAS credentials live; the NAS hashes the files, so only listings and digests
cross the network. A dry run unless told otherwise:

    # what would change
    temporal workflow start --task-queue fishsense_orchestrator \\
        --type RepairMovedCapturePathsWorkflow \\
        --workflow-id repair-paths-<dive number> --input <dive number>

    # change it
    ... --workflow-id repair-paths-<dive number>-apply \\
        --input <dive number> --input true

The activity's rules are in `ops.paths.activities`. Here: one dive per run,
the checksum verifier's budget and bounded retry (a retried apply finds the
rows it already moved at their new paths), and an unknown dive or a lost
membership is not retried.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ops.paths.contracts import (
        PathRepairReport,
    )

__all__ = ["RepairMovedCapturePathsWorkflow"]

# A dive is a few hundred listings and NAS-side hashes, driven serially.
_PER_DIVE_TIMEOUT = timedelta(hours=6)
# One frame at a time, so a gap this long means a hash is wedged.
_HEARTBEAT_TIMEOUT = timedelta(minutes=20)
_REPAIR_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=["DiveNotFound", "NotAMember"],
)


@workflow.defn
class RepairMovedCapturePathsWorkflow:
    # pylint: disable=too-few-public-methods
    """Report (and, with ``apply``, repair) one dive's moved captures."""

    @workflow.run
    async def run(self, dive_number: int, apply: bool = False) -> PathRepairReport:
        return await workflow.execute_activity(
            "repair_moved_capture_paths",
            args=(dive_number, apply),
            result_type=PathRepairReport,
            schedule_to_close_timeout=_PER_DIVE_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=_REPAIR_RETRY,
        )
