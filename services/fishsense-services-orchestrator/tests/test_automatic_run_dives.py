"""A by-hand automatic run of named dives: plan, review, apply.

New in v2 (Phase C, 2026-10-18). The validation runs the paper's dives
(cscw-fishsense2027 §6) through the production chain, by number. Like the
laser remediation: `dry-run` plans and writes nothing; `apply` takes the
reviewed report, re-plans, refuses if the plan changed (its digest), then
runs each dive's `AutomaticResultsForDiveWorkflow` in the plan's order --
calibration sessions first -- and reports each. One dive failing does not
stop the others.
"""

from __future__ import annotations

import uuid
from typing import List

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_orchestrator.automatic_results import run_dives as sut

QUEUE = "test-automatic-run-dives"
TENANT = uuid.uuid4()
DIVES = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
RAN: List[str] = []


def _plan(order=DIVES) -> dict:
    return {
        "tenant_id": str(TENANT),
        "dives": [
            {"number": 60 + i, "dive_id": str(d), "folder": f"/d/{i}",
             "calibration_session": i == 0, "excluded": False,
             "human_measured": True, "rectifiable": True,
             "frames": 10, "frames_owed": 10}
            for i, d in enumerate(order)
        ],
        "missing": [999],
    }  # fmt: skip


@activity.defn(name="plan_automatic_run")
async def _planner(tenant: str, numbers: List[int]) -> dict:
    return _plan()


NO_GPU: List[str] = []


@workflow.defn(name="AutomaticResultsForDiveWorkflow")
class _ForDive:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: dict) -> dict:
        RAN.append(workflow.info().workflow_id)
        if target["dive_id"] in NO_GPU:
            return {
                "tenant_id": target["tenant_id"],
                "dive_id": target["dive_id"],
                "skipped": ["frames: no GPU (unavailable)"],
                "failed": None,
                "species_failed": False,
                "no_gpu": "unavailable",
            }
        if target["dive_id"] == str(DIVES[1]):
            raise ApplicationError(
                "boom", type="AutomaticStepFailed", non_retryable=True
            )
        return {
            "tenant_id": target["tenant_id"],
            "dive_id": target["dive_id"],
            "skipped": [],
            "failed": None,
            "species_failed": False,
            "no_gpu": None,
        }


async def _run(request: sut.RunAutomaticDivesInput) -> dict:
    RAN.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=[_planner],
                          workflows=[sut.RunAutomaticDivesWorkflow, _ForDive],
                          # The stub child records into this module's RAN.
                          workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                sut.RunAutomaticDivesWorkflow.run, request,
                id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE, result_type=dict,
            )  # fmt: skip


async def test_a_dry_run_plans_and_runs_nothing():
    report = await _run(sut.RunAutomaticDivesInput(tenant="lab", numbers=[60, 61, 62]))

    assert report["mode"] == "dry_run"
    assert [d["dive_id"] for d in report["plan"]["dives"]] == [str(d) for d in DIVES]
    assert report["plan"]["missing"] == [999]
    assert report["plan_sha256"] == sut.plan_digest(_plan())
    assert RAN == [] and report["dives"] == []


async def test_apply_runs_each_dive_in_order_and_one_failure_stops_none():
    report = await _run(sut.RunAutomaticDivesInput(
        tenant="lab", numbers=[60, 61, 62], apply=True,
        expected_plan_sha256=sut.plan_digest(_plan())))  # fmt: skip

    assert report["mode"] == "apply"
    assert RAN == [f"automatic-results-{d}" for d in DIVES]
    assert [d["dive_id"] for d in report["dives"]] == [str(d) for d in DIVES]
    assert report["dives"][1]["error"] and not report["dives"][0]["error"]
    assert report["dives"][2]["report"]["failed"] is None


async def test_apply_refuses_a_plan_that_changed():
    with pytest.raises(WorkflowFailureError):
        await _run(sut.RunAutomaticDivesInput(
            tenant="lab", numbers=[60, 61, 62], apply=True,
            expected_plan_sha256="0" * 64))  # fmt: skip
    assert RAN == []


def test_the_digest_covers_the_dives_and_their_order_not_the_counts():
    """Counts drift as frames are done; which dives, in what order, as
    sessions or not, must not."""
    assert sut.plan_digest(_plan()) != sut.plan_digest(_plan(order=DIVES[::-1]))
    drifted = _plan()
    drifted["dives"][0]["frames_owed"] = 3
    assert sut.plan_digest(drifted) == sut.plan_digest(_plan())


def test_the_paper_dives_are_the_sessions_pool_reef_and_parked():
    from fishsense_services_api.automatic_validation import (
        PAPER_CALIBRATION_SESSIONS, PAPER_POOL_DIVES, PAPER_REEF_DIVES,
    )  # fmt: skip

    dives = sut.parse_args(["dry-run", "--tenant", "lab", "--paper", "--out", "x"])
    numbers = sut.numbers(dives)
    assert set(numbers) == {*PAPER_CALIBRATION_SESSIONS, *PAPER_POOL_DIVES,
                            *PAPER_REEF_DIVES, *sut.PARKED_DIVES}  # fmt: skip
    assert len(numbers) == len(set(numbers)) == 30
    assert sut.PARKED_DIVES == (427, 437, 526)


def test_apply_takes_its_dives_and_digest_from_the_reviewed_report(tmp_path):
    report = tmp_path / "plan.json"
    report.write_text('{"mode": "dry_run", "tenant": "lab", "numbers": [62, 58],'
                      ' "plan_sha256": "abc"}')  # fmt: skip
    request = sut.build_request(sut.parse_args(["apply", "--report", str(report)]))
    assert (request.tenant, request.numbers, request.apply,
            request.expected_plan_sha256) == ("lab", [62, 58], True, "abc")  # fmt: skip
    applied = tmp_path / "applied.json"
    applied.write_text('{"mode": "apply"}')
    with pytest.raises(SystemExit):
        sut.build_request(sut.parse_args(["apply", "--report", str(applied)]))


async def test_apply_stops_at_the_first_dive_with_no_gpu():
    """2026-10-18, the first validation run: the GPU never came up, and
    each dive waited out the GPU wake, skipped its frames and "completed",
    so the run walked on through every dive doing nothing. The backlog's
    drain stops on a GPU skip; so does this."""
    NO_GPU[:] = [str(DIVES[0])]
    try:
        report = await _run(sut.RunAutomaticDivesInput(
            tenant="lab", numbers=[60, 61, 62], apply=True,
            expected_plan_sha256=sut.plan_digest(_plan())))  # fmt: skip
    finally:
        NO_GPU.clear()

    assert RAN == [f"automatic-results-{DIVES[0]}"]
    assert report["stopped"] == "no GPU (unavailable)"
    assert [d["dive_id"] for d in report["dives"]] == [str(DIVES[0])]
