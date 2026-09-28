"""The head/tail stage: what it serves, and v1's schedules.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_schedule_registration.py (the head/tail rows) and worker.py's
`schedule_workflows`. v1's intervals, minutes, overlap and run timeouts:

* stage 5.1 at +30, SKIP, 1 h;
* predict at +32 (after the render it reads), SKIP; v1's 2 h run timeout
  is v2's every step at its longest (pinned below);
* populate at +34 (after predict, since it is prediction-gated), SKIP, 1 h;
* the label sync on the hour, overlap allowed (the cursor only moves
  forward), 3 h.

v2: the schedule ids are v2's own (Temporal is shared until cutover), and the
Create and Backfill workflows are served, on demand, as in v1.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_orchestrator.headtail import workflow as wf
from fishsense_services_orchestrator.nrp.workflow import GPU_WAKE_TIMEOUT
from fishsense_services_orchestrator.registry import stages
from fishsense_services_orchestrator.schedules import ensure_schedules

QUEUE = "test-headtail-schedules"

EXPECTED = {
    "preprocess-headtail-images": (
        "PreprocessHeadtailImagesParentWorkflow", timedelta(minutes=30),
        ScheduleOverlapPolicy.SKIP, timedelta(hours=1),
    ),
    "predict-headtail-images": (
        "PredictHeadtailImagesParentWorkflow", timedelta(minutes=32),
        ScheduleOverlapPolicy.SKIP, wf.PREDICT_RUN_TIMEOUT,
    ),
    "populate-headtail-labels": (
        "PopulateHeadTailLabelStudioProjectParentWorkflow", timedelta(minutes=34),
        ScheduleOverlapPolicy.SKIP, timedelta(hours=1),
    ),
    "sync-label-studio-head-tail-labels": (
        "SyncLabelStudioHeadTailLabelsWorkflow", timedelta(0),
        ScheduleOverlapPolicy.ALLOW_ALL, timedelta(hours=3),
    ),
}  # fmt: skip


def _stage():
    (stage,) = [s for s in stages() if s.name == "headtail"]
    return stage


def test_the_stage_serves_every_head_tail_workflow():
    assert set(_stage().workflows) == {
        wf.PreprocessHeadtailImagesParentWorkflow,
        wf.PredictHeadtailImagesParentWorkflow,
        wf.PopulateHeadTailLabelStudioProjectParentWorkflow,
        wf.PopulateHeadTailLabelStudioProjectWorkflow,
        wf.CreateHeadTailLabelStudioProjectWorkflow,
        wf.BackfillHeadtailPredictionsWorkflow,
        wf.SyncLabelStudioHeadTailLabelsWorkflow,
    }


def test_the_schedules_are_v1s_by_value():
    schedules = {s.schedule_id: s for s in _stage().schedules}
    assert set(schedules) == set(EXPECTED)
    for schedule_id, (name, offset, overlap, run_timeout) in EXPECTED.items():
        scheduled = schedules[schedule_id]
        assert scheduled.workflow.__name__ == name
        assert scheduled.every == timedelta(hours=1)
        assert (scheduled.offset, scheduled.overlap, scheduled.run_timeout) == (
            offset,
            overlap,
            run_timeout,
        ), schedule_id


def test_the_predict_run_outlives_every_step_it_waits_on():
    """v2: v1's 2 h run timeout was shorter than its own 6 h child. When the
    run times out, Temporal terminates the child with it (the parent-close
    policy), so a long CPU-fallback dive ran for 2 h and persisted nothing,
    every firing. The run must cover each step at its longest: the select,
    the resolve, the GPU wake, the child, the persist and the backfill."""
    (predict,) = [
        s for s in _stage().schedules if s.schedule_id == "predict-headtail-images"
    ]
    longest = (
        wf.PREDICT_SELECT_TIMEOUT
        + wf.PREDICT_RESOLVE_TIMEOUT
        + GPU_WAKE_TIMEOUT
        + wf.PREDICT_CHILD_TIMEOUT
        + wf.PREDICT_PERSIST_TIMEOUT
        + wf.PREDICT_BACKFILL_TIMEOUT
    )

    assert predict.run_timeout >= longest
    assert predict.overlap == ScheduleOverlapPolicy.SKIP, "one dive at a time"


@pytest.mark.parametrize("schedule_id", sorted(EXPECTED))
async def test_each_schedule_is_created_as_declared(schedule_id):
    name, offset, overlap, run_timeout = EXPECTED[schedule_id]
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        await ensure_schedules(env.client, task_queue=QUEUE)
        schedule = (
            await env.client.get_schedule_handle(schedule_id).describe()
        ).schedule

    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(hours=1)
    assert (interval.offset or timedelta(0)) == offset
    assert schedule.policy.overlap == overlap
    assert schedule.action.workflow == name
    assert schedule.action.task_queue == QUEUE
    assert schedule.action.run_timeout == run_timeout
