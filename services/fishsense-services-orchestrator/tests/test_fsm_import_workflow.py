"""The FSM import workflow: on demand, a dry run unless told, and an apply
names the plan it reviewed (the command is in the workflow's docstring)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fishsense_services_contracts.temporal import DATA_CONVERTER
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.ops.mobile_import.contracts import (
    FsmImportReport,
    FsmImportRequest,
)
from fishsense_services_orchestrator.ops.mobile_import.workflow import (
    ImportFsmExportWorkflow,
)

QUEUE = "test-fsm-import"
REQUEST = FsmImportRequest(
    dataset="/fishsense_data/CCFRP/2025.06.27.FishSense.SanDiego",
    tenant="ccfrp-ucsd",
    captured_from=date(2025, 6, 20),
    captured_to=date(2025, 9, 22),
    time_zone="America/Los_Angeles",
    intrinsics={"iPad": [1.0] * 9},
    intrinsics_source="test",
)


def _report(applied: bool) -> FsmImportReport:
    return FsmImportReport(applied=applied, plan_sha256="a" * 64, planned=1,
                           by_day={}, by_phone={}, by_folder={}, excluded={})  # fmt: skip


async def _execute(args):
    calls: list[tuple] = []

    @activity.defn(name="plan_fsm_import")
    async def plan(request: FsmImportRequest) -> FsmImportReport:
        calls.append(("plan", request.tenant))
        return _report(False)

    @activity.defn(name="import_fsm_captures")
    async def apply(request: FsmImportRequest, expected: str) -> FsmImportReport:
        calls.append(("import", request.tenant, expected))
        return _report(True)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE,
                          workflows=[ImportFsmExportWorkflow],
                          activities=[plan, apply]):  # fmt: skip
            report = await env.client.execute_workflow(
                ImportFsmExportWorkflow.run, args=args,
                id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE,
            )  # fmt: skip
    return report, calls


async def test_the_request_alone_is_a_dry_run():
    report, calls = await _execute((REQUEST,))

    assert calls == [("plan", "ccfrp-ucsd")]
    assert report.applied is False


async def test_apply_passes_the_reviewed_plan():
    report, calls = await _execute((REQUEST, True, "b" * 64))

    assert calls == [("import", "ccfrp-ucsd", "b" * 64)]
    assert report.applied is True


async def test_apply_without_a_reviewed_plan_is_refused():
    with pytest.raises(WorkflowFailureError):
        await _execute((REQUEST, True))
