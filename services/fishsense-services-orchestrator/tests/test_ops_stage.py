"""Operations, as a stage: checksum verification and the labeling-config
reconcile, with the reconcile's schedule.

Ported from the ops half of fishsense-lite@77e8f8e5 services/
fishsense-api-workflow-worker/tests/test_schedule_registration.py and
test_activity_registration.py. v1's schedule, kept: hourly at :25, skipping on
overlap (a slow pass must not stack), a 30-minute run timeout. The checksum
workflows have no schedule; an operator starts them.

v2 change: the schedule id drops v1's ``-workflow-schedule`` suffix, as every v2
schedule does, so it never collides with v1's on the shared Temporal (PLAN.md
§6.5).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_orchestrator.ops.checksums.workflows import (
    VerifyAllDivesChecksumsWorkflow,
    VerifyDiveChecksumsWorkflow,
)
from fishsense_services_orchestrator.ops.labeling_configs.workflow import (
    ReconcileLabelingConfigsWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, stages
from fishsense_services_orchestrator.schedules import ensure_schedules

from .test_registry import NAS


def _ops():
    (stage,) = [s for s in stages() if s.name == "ops"]
    return stage


def test_the_ops_stage_serves_its_three_workflows():
    assert set(_ops().workflows) == {
        VerifyDiveChecksumsWorkflow,
        VerifyAllDivesChecksumsWorkflow,
        ReconcileLabelingConfigsWorkflow,
    }


def test_its_activities_build_from_the_nas_and_label_studio_settings(monkeypatch):
    for name, value in NAS.items():
        monkeypatch.setenv(name, value)

    built = _ops().build_activities(Deps(engine=None, sub="service:orchestrator"))

    assert {a.__temporal_activity_definition.name for a in built} == {
        "verify_dive_checksums",
        "select_canonical_dive_numbers",
        "reconcile_labeling_configs",
    }


def test_only_the_reconcile_is_scheduled():
    (schedule,) = _ops().schedules
    assert schedule.workflow is ReconcileLabelingConfigsWorkflow


async def test_the_reconcile_runs_hourly_at_25_and_skips_overlap():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        await ensure_schedules(env.client, task_queue="test-ops")
        described = await env.client.get_schedule_handle(
            "reconcile-labeling-configs"
        ).describe()

    schedule = described.schedule
    (interval,) = schedule.spec.intervals
    assert interval.every == timedelta(hours=1)
    assert interval.offset == timedelta(minutes=25)
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.action.workflow == "ReconcileLabelingConfigsWorkflow"
    assert schedule.action.run_timeout == timedelta(minutes=30)
