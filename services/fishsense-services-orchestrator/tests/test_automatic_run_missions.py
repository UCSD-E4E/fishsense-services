"""A by-hand automatic run of named dives: plan, review, apply.

New in v2 (Phase C, 2026-10-18). The validation runs the paper's dives
(cscw-fishsense2027 §6) through the production chain, by number. Like the
laser remediation: `dry-run` plans and writes nothing; `apply` takes the
reviewed report, re-plans, refuses if the plan changed (its digest), then
runs each dive's `AutomaticResultsForMissionWorkflow` in the plan's order --
calibration sessions first -- and reports each. One dive failing does not
stop the others.
"""

from __future__ import annotations

import uuid
from typing import List

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from fishsense_services_contracts.temporal import DATA_CONVERTER
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_orchestrator.automatic_results import run_missions as sut

QUEUE = "test-automatic-run-dives"
TENANT = uuid.uuid4()
MISSIONS = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
RAN: List[str] = []


def _plan(order=MISSIONS) -> dict:
    return {
        "tenant_id": str(TENANT),
        "missions": [
            {"number": 60 + i, "mission_id": str(d), "folder": f"/d/{i}",
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
REFUSED: List[str] = []


@workflow.defn(name="AutomaticResultsForDiveWorkflow")
class _ForMission:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: dict) -> dict:
        RAN.append(workflow.info().workflow_id)
        if target["dive_id"] in NO_GPU:
            return {
                "tenant_id": target["tenant_id"],
                "mission_id": target["dive_id"],
                "skipped": ["frames: no GPU (unavailable)"],
                "failed": None,
                "species_failed": False,
                "no_gpu": "unavailable",
            }
        if target["dive_id"] in REFUSED:
            raise ApplicationError(
                "frames: Sam3WeightsCorrupt: ...", "Sam3WeightsCorrupt",
                type="AutomaticWorkerRefused", non_retryable=True,
            )  # fmt: skip
        if target["dive_id"] == str(MISSIONS[1]):
            raise ApplicationError(
                "boom", type="AutomaticStepFailed", non_retryable=True
            )
        return {
            "tenant_id": target["tenant_id"],
            "mission_id": target["dive_id"],
            "skipped": [],
            "failed": None,
            "species_failed": False,
            "no_gpu": None,
        }


async def _run(request: sut.RunAutomaticMissionsInput) -> dict:
    RAN.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=[_planner],
                          workflows=[sut.RunAutomaticMissionsWorkflow, _ForMission],
                          # The stub child records into this module's RAN.
                          workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                sut.RunAutomaticMissionsWorkflow.run, request,
                id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE, result_type=dict,
            )  # fmt: skip


async def test_a_dry_run_plans_and_runs_nothing():
    report = await _run(
        sut.RunAutomaticMissionsInput(tenant="lab", numbers=[60, 61, 62])
    )

    assert report["mode"] == "dry_run"
    assert [d["mission_id"] for d in report["plan"]["missions"]] == [
        str(d) for d in MISSIONS
    ]
    assert report["plan"]["missing"] == [999]
    assert report["plan_sha256"] == sut.plan_digest(_plan())
    assert RAN == [] and report["missions"] == []


async def test_apply_runs_each_mission_in_order_and_one_failure_stops_none():
    report = await _run(sut.RunAutomaticMissionsInput(
        tenant="lab", numbers=[60, 61, 62], apply=True,
        expected_plan_sha256=sut.plan_digest(_plan())))  # fmt: skip

    assert report["mode"] == "apply"
    assert RAN == [f"automatic-results-{d}" for d in MISSIONS]
    assert [d["mission_id"] for d in report["missions"]] == [str(d) for d in MISSIONS]
    assert report["missions"][1]["error"] and not report["missions"][0]["error"]
    assert report["missions"][2]["report"]["failed"] is None


async def test_apply_refuses_a_plan_that_changed():
    with pytest.raises(WorkflowFailureError):
        await _run(sut.RunAutomaticMissionsInput(
            tenant="lab", numbers=[60, 61, 62], apply=True,
            expected_plan_sha256="0" * 64))  # fmt: skip
    assert RAN == []


def test_the_digest_covers_the_missions_and_their_order_not_the_counts():
    """Counts drift as frames are done; which dives, in what order, as
    sessions or not, must not."""
    assert sut.plan_digest(_plan()) != sut.plan_digest(_plan(order=MISSIONS[::-1]))
    drifted = _plan()
    drifted["missions"][0]["frames_owed"] = 3
    assert sut.plan_digest(drifted) == sut.plan_digest(_plan())


def test_the_paper_missions_are_the_sessions_pool_reef_and_parked():
    from fishsense_services_api.automatic_validation import (
        PAPER_CALIBRATION_SESSIONS, PAPER_POOL_MISSIONS, PAPER_REEF_MISSIONS,
    )  # fmt: skip

    missions = sut.parse_args(["dry-run", "--tenant", "lab", "--paper", "--out", "x"])
    numbers = sut.numbers(missions)
    assert set(numbers) == {*PAPER_CALIBRATION_SESSIONS, *PAPER_POOL_MISSIONS,
                            *PAPER_REEF_MISSIONS, *sut.PARKED_MISSIONS}  # fmt: skip
    assert len(numbers) == len(set(numbers)) == 30
    assert sut.PARKED_MISSIONS == (427, 437, 526)


def test_apply_takes_its_missions_and_digest_from_the_reviewed_report(tmp_path):
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


async def test_apply_stops_at_the_first_mission_with_no_gpu():
    """2026-10-18, the first validation run: the GPU never came up, and
    each dive waited out the GPU wake, skipped its frames and "completed",
    so the run walked on through every dive doing nothing. The backlog's
    drain stops on a GPU skip; so does this."""
    NO_GPU[:] = [str(MISSIONS[0])]
    try:
        report = await _run(sut.RunAutomaticMissionsInput(
            tenant="lab", numbers=[60, 61, 62], apply=True,
            expected_plan_sha256=sut.plan_digest(_plan())))  # fmt: skip
    finally:
        NO_GPU.clear()

    assert RAN == [f"automatic-results-{MISSIONS[0]}"]
    assert report["stopped"] == "no GPU (unavailable)"
    assert [d["mission_id"] for d in report["missions"]] == [str(MISSIONS[0])]


async def test_apply_stops_at_the_first_mission_the_processor_refuses():
    """2026-10-24: a processor whose weights cannot load (or with no GPU)
    refuses every dive alike; the run stops at the first, and says why,
    rather than walking every dive into the same refusal."""
    REFUSED[:] = [str(MISSIONS[0])]
    try:
        report = await _run(sut.RunAutomaticMissionsInput(
            tenant="lab", numbers=[60, 61, 62], apply=True,
            expected_plan_sha256=sut.plan_digest(_plan())))  # fmt: skip
    finally:
        REFUSED.clear()

    assert RAN == [f"automatic-results-{MISSIONS[0]}"]
    # Named, as the drain names it (review of #364).
    assert report["stopped"] == "worker refused (Sam3WeightsCorrupt)"
    assert "Sam3WeightsCorrupt" in report["missions"][0]["error"]


def test_a_plan_from_before_the_rename_reads_the_same_and_keeps_its_digest():
    """A run planned before Phase 2a (in a running workflow's history, or
    reviewed before the deploy) names its missions `dives` / `dive_id`. Read
    under today's names, its digest -- the one the operator approved -- holds."""
    from fishsense_services_orchestrator.automatic_results.run_missions import (
        plan_digest,
        with_current_names,
    )

    mission = "00000000-0000-0000-0000-000000000001"
    old = {
        "tenant_id": "t",
        "dives": [{"number": 7, "dive_id": mission, "calibration_session": False}],
    }
    new = {
        "tenant_id": "t",
        "missions": [
            {"number": 7, "mission_id": mission, "calibration_session": False}
        ],
    }

    assert with_current_names(old) == with_current_names(new) == new
    assert plan_digest(with_current_names(old)) == plan_digest(new)


def test_the_old_flag_still_names_missions():
    from fishsense_services_orchestrator.automatic_results import run_missions

    args = run_missions.parse_args(
        ["dry-run", "--tenant", "lab", "--dives", "62,58", "--out", "p.json"]
    )

    assert run_missions.numbers(args) == [62, 58]


def test_the_old_module_path_still_runs():
    """Runbooks say `python -m ...automatic_results.run_dives`."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m",
         "fishsense_services_orchestrator.automatic_results.run_dives", "--help"],
        capture_output=True, text=True,
    )  # fmt: skip

    assert done.returncode == 0, done.stderr
    assert "run_missions" in done.stdout
