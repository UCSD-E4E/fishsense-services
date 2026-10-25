"""Read every NAS frame's EXIF focal length, for the zoom gate: on demand.

New in v2 (2026-10-25). It runs in the orchestrator, in the slot, where the
NAS credentials live, and reads 4 KB a frame. A dry run unless told otherwise:

    # what is there (reads, writes nothing)
    temporal workflow start --task-queue fishsense_orchestrator \\
        --type BackfillFocalLengthsWorkflow --workflow-id focal-lengths

    # record it
    ... --workflow-id focal-lengths-apply --input true

    # named missions only
    ... --input false --input '[374, 384, 386]'

The activity's rules are in `ops.focal_lengths.activities`. Here: one mission
at a time (the NAS is one shared, slow stream), the path repair's budget and
bounded retry per mission (a retried apply skips the frames it recorded), and
a lost membership is not retried.
"""

from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.ops.focal_lengths.contracts import (
        FocalLengthBackfillReport,
        FocalLengthMissionRef,
        FocalLengthMissionReport,
    )

__all__ = ["BackfillFocalLengthsWorkflow"]

# A mission is a few hundred 4 KB reads, serially.
_PER_MISSION_TIMEOUT = timedelta(hours=2)
# One frame at a time, so a gap this long means a read is wedged.
_HEARTBEAT_TIMEOUT = timedelta(minutes=10)
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=["NotAMember"],
)


@workflow.defn
class BackfillFocalLengthsWorkflow:
    # pylint: disable=too-few-public-methods
    """Report (and, with ``apply``, record) the focal lengths of frames
    ingested before ingest kept them."""

    @workflow.run
    async def run(
        self, apply: bool = False, missions: Optional[List[int]] = None
    ) -> FocalLengthBackfillReport:
        refs = await workflow.execute_activity(
            "focal_length_missions",
            result_type=List[FocalLengthMissionRef],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY,
        )
        if missions is not None:
            wanted = set(missions)
            refs = [r for r in refs if r.mission_number in wanted]
        report = FocalLengthBackfillReport(applied=apply)
        for ref in refs:
            report.missions.append(
                await workflow.execute_activity(
                    "read_mission_focal_lengths",
                    args=(ref, apply),
                    result_type=FocalLengthMissionReport,
                    schedule_to_close_timeout=_PER_MISSION_TIMEOUT,
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                    retry_policy=_RETRY,
                )
            )
        return report
