"""The focal-length backfill workflow: on demand, a dry run unless told.

    temporal workflow start --task-queue fishsense_orchestrator \\
        --type BackfillFocalLengthsWorkflow                          # dry run
    ... --input true                                                 # apply
    ... --input false --input '[374, 384]'                           # these only
"""

from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.ops.focal_lengths.contracts import (
    FocalLengthBackfillReport,
    FocalLengthMissionRef,
    FocalLengthMissionReport,
)
from fishsense_services_orchestrator.ops.focal_lengths.workflow import (
    BackfillFocalLengthsWorkflow,
)

QUEUE = "test-focal-lengths"


def _ref(number):
    return FocalLengthMissionRef(
        tenant_id=str(uuid.UUID(int=1)), mission_id=str(uuid.UUID(int=number)),
        mission_number=number, frames=2,
    )  # fmt: skip


async def _execute(args):
    calls: list[tuple] = []

    @activity.defn(name="focal_length_missions")
    async def missions() -> list[FocalLengthMissionRef]:
        return [_ref(374), _ref(384), _ref(436)]

    @activity.defn(name="read_mission_focal_lengths")
    async def read(
        mission: FocalLengthMissionRef, apply: bool
    ) -> FocalLengthMissionReport:
        calls.append((mission.mission_number, apply))
        return FocalLengthMissionReport(
            mission_number=mission.mission_number, applied=apply, frames=2, zoomed=1
        )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client, task_queue=QUEUE, workflows=[BackfillFocalLengthsWorkflow],
            activities=[missions, read],
        ):  # fmt: skip
            report = await env.client.execute_workflow(
                BackfillFocalLengthsWorkflow.run,
                args=args,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
    return report, calls


async def test_no_input_is_a_dry_run_over_every_mission():
    report, calls = await _execute(())

    assert calls == [(374, False), (384, False), (436, False)]
    assert isinstance(report, FocalLengthBackfillReport)
    assert report.applied is False
    assert (report.frames, report.zoomed) == (6, 3)


async def test_apply_is_explicit():
    report, calls = await _execute((True,))

    assert report.applied is True
    assert {apply for _, apply in calls} == {True}


async def test_named_missions_only():
    _, calls = await _execute((False, [384]))

    assert calls == [(384, False)]
