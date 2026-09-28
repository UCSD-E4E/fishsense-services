"""The slate and calibration stages' schedules keep v1's minutes and policies.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_schedule_registration.py (the stage-9, slate-sync, stage-13 and
checkerboard rows, and the stagger). v1's reasons: hourly selectors are
offset across the hour so they don't all hit the database at once; a
selector's schedule skips on overlap so two firings never pick one dive; the
sync allows overlap because its cursor only moves forward.

Not ported: v1 actively deleted the retired slate predictor's schedule
(`_RETIRED_SCHEDULE_IDS`). v2 never created it, so there is nothing to retire;
and the lattice study was never scheduled, in v1 or here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_orchestrator.calibration.workflows import (
    VerifyCheckerboardLatticeParentWorkflow,
)
from fishsense_services_orchestrator.schedules import SCHEDULES, ensure_schedules

QUEUE = "test-slate-calibration-schedules"

#: schedule id -> (workflow, minute, run timeout, overlap)
EXPECTED = {
    "preprocess-slate-images": (
        "PreprocessSlateImagesParentWorkflow",
        timedelta(minutes=45),
        timedelta(hours=1),
        ScheduleOverlapPolicy.SKIP,
    ),
    "sync-label-studio-dive-slate-labels": (
        "SyncLabelStudioDiveSlateLabelsWorkflow",
        timedelta(0),
        timedelta(hours=3),
        ScheduleOverlapPolicy.ALLOW_ALL,
    ),
    "perform-laser-calibration": (
        "PerformLaserCalibrationParentWorkflow",
        timedelta(minutes=50),
        timedelta(minutes=30),
        ScheduleOverlapPolicy.SKIP,
    ),
    "perform-checkerboard-calibration": (
        "PerformCheckerboardCalibrationParentWorkflow",
        timedelta(minutes=52),
        timedelta(hours=3),
        ScheduleOverlapPolicy.SKIP,
    ),
}


@pytest.fixture(name="described")
async def described_fixture():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        await ensure_schedules(env.client, task_queue=QUEUE)
        yield {
            schedule_id: (
                await env.client.get_schedule_handle(schedule_id).describe()
            ).schedule
            for schedule_id in EXPECTED
        }


@pytest.mark.parametrize("schedule_id", sorted(EXPECTED))
async def test_each_schedule_keeps_v1s_minute_timeout_and_overlap(
    described, schedule_id
):
    workflow, minute, run_timeout, overlap = EXPECTED[schedule_id]
    schedule = described[schedule_id]

    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(hours=1)
    assert (interval.offset or timedelta(0)) == minute
    assert schedule.policy.overlap == overlap
    assert schedule.action.workflow == workflow
    assert schedule.action.run_timeout == run_timeout
    assert schedule.action.task_queue == QUEUE


def test_the_calibration_producers_fire_in_v1s_order():
    """Stage 13 (+50) before the checkerboard (+52), both after stage 9
    (+45): a dive both could fit is stage 13's, and the two minutes apart
    keep their selectors off the database together."""
    minutes = {s.schedule_id: s.offset for s in SCHEDULES if s.schedule_id in EXPECTED}
    assert (
        minutes["preprocess-slate-images"]
        < minutes["perform-laser-calibration"]
        < minutes["perform-checkerboard-calibration"]
    )


def test_the_lattice_study_is_never_scheduled():
    """An experiment's population is chosen by a person, not a predicate."""
    assert VerifyCheckerboardLatticeParentWorkflow not in {
        s.workflow for s in SCHEDULES
    }
