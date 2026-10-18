"""Operator CLI and workflow: run named dives through the automatic chain.

New in v2 (Phase C, 2026-10-18). The validation runs the paper's dives
(cscw-fishsense2027@96a8da07 §6) through the production chain by number,
before the backlog is enabled. Shaped as the laser remediation
(`laser.remediate`): run where the orchestrator's Temporal settings are
(``FISHSENSE_TEMPORAL_*``)::

    python -m fishsense_services_orchestrator.automatic_results.run_dives \\
        dry-run --tenant lab --paper --out /tmp/plan.json   # or --dives 62,58

    python -m fishsense_services_orchestrator.automatic_results.run_dives \\
        apply --report /tmp/plan.json

`dry-run` writes nothing: the plan names each dive's folder, whether it is a
calibration session, excluded (by hand it still runs, 0039) or
human-measured, and the frames it owes; numbers with no dive are listed. The
order is calibration sessions first -- their slate frames feed the
label-free fits their borrowers use -- then the order given. `apply` takes
the tenant, the numbers and the plan's digest from the reviewed report and
nothing else; the workflow re-plans and refuses unless the digest still
matches (the dives, their order and which are sessions; not the frame
counts, which drift as frames are done). Then it runs each dive's
`AutomaticResultsForDiveWorkflow` in turn -- the same child id a by-hand run
uses -- and reports each: one dive failing (its failure is recorded against
the dive) stops none of the others, but a dive whose GPU steps were skipped
for want of a GPU stops the run (`stopped`): every dive behind it would wait
out the wake and skip too. `apply` starts the run and returns its
workflow id; the report is the workflow's result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from temporalio import workflow

from fishsense_services_orchestrator.automatic_results.workflow import RUN_TIMEOUT
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ApplicationError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

__all__ = [
    "PARKED_DIVES",
    "RunAutomaticDivesInput",
    "RunAutomaticDivesWorkflow",
    "build_request",
    "main",
    "numbers",
    "parse_args",
    "plan_digest",
]

#: Parked dives that may recover label-free (decided 2026-10-15).
PARKED_DIVES = (427, 437, 526)
PLAN_TIMEOUT = timedelta(minutes=15)
#: A dive's every step at its longest: the backlog run's own timeout, built
#: from the GPU children's ceiling (review of #110: a fixed 20 h was shorter
#: than one large dive's frames at one a time).
DIVE_TIMEOUT = RUN_TIMEOUT


@dataclass
class RunAutomaticDivesInput:
    tenant: str
    numbers: List[int]
    apply: bool = False
    expected_plan_sha256: Optional[str] = None


def plan_digest(plan: dict) -> str:
    """The plan's identity: its tenant, and its dives in order, by id and
    whether each is a session."""
    key = [
        plan["tenant_id"],
        [(d["number"], d["dive_id"], d["calibration_session"]) for d in plan["dives"]],
    ]
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


@workflow.defn
class RunAutomaticDivesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: RunAutomaticDivesInput) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_automatic_run",
            args=[request.tenant, list(request.numbers)],
            schedule_to_close_timeout=PLAN_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=2),
            result_type=dict,
        )
        digest = plan_digest(plan)
        report = {
            "mode": "apply" if request.apply else "dry_run",
            "tenant": request.tenant,
            "numbers": list(request.numbers),
            "plan": plan,
            "plan_sha256": digest,
            "dives": [],
            "stopped": None,
        }
        if not request.apply:
            return report
        if digest != request.expected_plan_sha256:
            raise ApplicationError(
                f"the plan changed since its review ({request.expected_plan_sha256}"
                f" -> {digest}): dry-run again",
                type="PlanChanged",
                non_retryable=True,
            )
        for dive in plan["dives"]:
            target = {"tenant_id": plan["tenant_id"], "dive_id": dive["dive_id"]}
            row = {"number": dive["number"], "dive_id": dive["dive_id"],
                   "report": None, "error": None}  # fmt: skip
            try:
                row["report"] = await workflow.execute_child_workflow(
                    "AutomaticResultsForDiveWorkflow",
                    target,
                    id=f"automatic-results-{dive['dive_id']}",
                    execution_timeout=DIVE_TIMEOUT,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=dict,
                )
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            except WorkflowAlreadyStartedError:
                row["error"] = "already running (another by-hand run has it)"
            workflow.logger.info(
                "automatic run: dive %s %s", dive["number"], row["error"] or "done"
            )
            report["dives"].append(row)
            # With no GPU every dive behind this one would wait out the wake
            # and skip too (2026-10-18, the first validation run): stop, as
            # the backlog's drain does.
            no_gpu = (row["report"] or {}).get("no_gpu")
            if no_gpu:
                report["stopped"] = f"no GPU ({no_gpu})"
                break
        return report


# -- the CLI ------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.automatic_results.run_dives"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run", help="plan the dives; run nothing")
    dry.add_argument("--tenant", required=True, help="the tenant's slug")
    which = dry.add_mutually_exclusive_group(required=True)
    which.add_argument("--dives", help="comma-separated dive numbers")
    which.add_argument(
        "--paper",
        action="store_true",
        help="the paper's sessions, pool and reef dives, and the parked ones",
    )
    dry.add_argument("--out", required=True, help="where to write the plan")
    apply = sub.add_parser("apply", help="run a reviewed dry-run plan")
    apply.add_argument("--report", required=True, help="the reviewed plan")
    return parser.parse_args(argv)


def numbers(args: argparse.Namespace) -> list[int]:
    """The dives a dry run names, in the order given (duplicates dropped)."""
    if args.paper:
        # pylint: disable=import-outside-toplevel
        from fishsense_services_api.automatic_validation import (
            PAPER_CALIBRATION_SESSIONS,
            PAPER_POOL_DIVES,
            PAPER_REEF_DIVES,
        )

        named = [*PAPER_CALIBRATION_SESSIONS, *PAPER_POOL_DIVES, *PAPER_REEF_DIVES,
                 *PARKED_DIVES]  # fmt: skip
    else:
        named = [int(d) for d in args.dives.split(",") if d.strip()]
    return list(dict.fromkeys(named))


def build_request(args: argparse.Namespace) -> RunAutomaticDivesInput:
    if args.command == "apply":
        with open(args.report, encoding="utf-8") as handle:
            report = json.load(handle)
        if report.get("mode") != "dry_run":
            sys.exit(f"{args.report} is not a dry-run plan; refusing to apply it")
        return RunAutomaticDivesInput(
            tenant=report["tenant"],
            numbers=[int(n) for n in report["numbers"]],
            apply=True,
            expected_plan_sha256=report["plan_sha256"],
        )
    return RunAutomaticDivesInput(tenant=args.tenant, numbers=numbers(args))


def _summary(plan: dict) -> str:
    lines = [f"{'dive':>5}  {'session':<7} {'excl':<4} {'human':<5} "
             f"{'frames':>6} {'owed':>5}  folder"]  # fmt: skip
    for d in plan["dives"]:
        lines.append(
            f"{d['number']:>5}  {'yes' if d['calibration_session'] else '':<7} "
            f"{'yes' if d['excluded'] else '':<4} "
            f"{'yes' if d['human_measured'] else '':<5} "
            f"{d['frames']:>6} {d['frames_owed']:>5}  {d['folder']}"
            + ("" if d["rectifiable"] else "   (not rectifiable)")
        )
    if plan["missing"]:
        lines.append(f"no dive numbered: {plan['missing']}")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> None:
    # pylint: disable=import-outside-toplevel
    from temporalio.client import Client

    from fishsense_services_contracts.temporal import connect_options
    from fishsense_services_orchestrator.settings import TemporalSettings

    request = build_request(args)
    temporal = TemporalSettings()
    client = await Client.connect(**connect_options(temporal))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "apply" if request.apply else "dry-run"
    workflow_id = f"automatic-run-dives-{mode}-{stamp}"
    if request.apply:
        await client.start_workflow(
            RunAutomaticDivesWorkflow.run, request,
            id=workflow_id, task_queue=temporal.task_queue,
        )  # fmt: skip
        print(f"apply started: {workflow_id} (its result is the report)")
        return
    report = await client.execute_workflow(
        RunAutomaticDivesWorkflow.run, request,
        id=workflow_id, task_queue=temporal.task_queue, result_type=dict,
    )  # fmt: skip
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(_summary(report["plan"]))
    print(f"digest {report['plan_sha256']}; plan -> {args.out}")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(_run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
