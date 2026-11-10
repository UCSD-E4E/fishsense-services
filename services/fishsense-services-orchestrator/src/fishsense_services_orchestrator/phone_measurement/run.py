"""Operator CLI: measure a tenant's phone captures that were never measured,
as FishSense Mobile measures them (2026-11-09). Run where the orchestrator's
Temporal settings are::

    python -m fishsense_services_orchestrator.phone_measurement.run \\
        dry-run --tenant ccfrp-ucsd [--missions 3,4] --out /tmp/phone-plan.json

    python -m fishsense_services_orchestrator.phone_measurement.run \\
        apply --report /tmp/phone-plan.json

`dry-run` writes nothing: each mission with captures owed a measurement, how
many, and the plan's digest. `apply` starts the run, refused unless the
plan's digest still matches. Results are unvalidated (`phone_measurements`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from fishsense_services_orchestrator.phone_measurement.workflow import (
    PhoneMeasurementRequest,
    PhoneMeasurementRunWorkflow,
)

__all__ = ["build_request", "main", "parse_args"]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.phone_measurement.run"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run", help="plan the run; measure nothing")
    dry.add_argument("--tenant", required=True, help="the tenant's slug")
    dry.add_argument("--missions", default="", help="comma-separated mission numbers")
    dry.add_argument("--out", required=True, help="where to write the plan")
    apply = sub.add_parser("apply", help="run a reviewed plan")
    apply.add_argument("--report", required=True, help="the reviewed plan")
    return parser.parse_args(argv)


def build_request(args: argparse.Namespace) -> PhoneMeasurementRequest:
    if args.command == "apply":
        with open(args.report, encoding="utf-8") as handle:
            report = json.load(handle)
        if report.get("mode") != "dry_run":
            sys.exit(f"{args.report} is not a dry-run plan; refusing to apply it")
        return PhoneMeasurementRequest(
            tenant=report["tenant"], numbers=[int(n) for n in report["numbers"]],
            apply=True, expected_plan_sha256=report["plan_sha256"],
        )  # fmt: skip
    numbers = [int(n) for n in args.missions.split(",") if n.strip()]
    return PhoneMeasurementRequest(
        tenant=args.tenant, numbers=list(dict.fromkeys(numbers))
    )


def _summary(report: dict) -> str:
    missions = report["plan"]["missions"]
    lines = [f"phone captures owed a measurement: {report['captures']} in "
             f"{len(missions)} missions"]  # fmt: skip
    lines += [f"  mission {m['number']}: {m['captures']}" for m in missions]
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
    if request.apply:
        workflow_id = f"phone-measurement-{request.tenant}-apply-{stamp}"
        await client.start_workflow(PhoneMeasurementRunWorkflow.run, request,
                                    id=workflow_id, task_queue=temporal.task_queue)  # fmt: skip
        print(f"apply started: {workflow_id} (its result is the report)")
        return
    report = await client.execute_workflow(
        PhoneMeasurementRunWorkflow.run, request,
        id=f"phone-measurement-{request.tenant}-dry-run-{stamp}",
        task_queue=temporal.task_queue, result_type=dict,
    )  # fmt: skip
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(_summary(report))
    print(f"digest {report['plan_sha256']}; plan -> {args.out}")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(_run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
