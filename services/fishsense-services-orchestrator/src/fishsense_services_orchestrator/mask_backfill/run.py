"""Operator CLI: backfill SAM masks for predictions made before they were
stored (2026-10-22). Run where the orchestrator's Temporal settings are::

    python -m fishsense_services_orchestrator.mask_backfill.run \\
        dry-run --tenant lab --source automatic [--missions 62,58] \\
        [--parts 3] --out /tmp/masks-plan.json

    python -m fishsense_services_orchestrator.mask_backfill.run \\
        apply --report /tmp/masks-plan.json [--part 0]

`dry-run` writes nothing: each mission owed masks and how many, the digest,
and the plan split into `--parts` of about equal predictions. `apply` starts
one part's run (`--part`, default 0; one apply per part to run them side by
side), refused unless the plan's digest still matches.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from fishsense_services_orchestrator.mask_backfill.workflow import (
    MaskBackfillRequest,
    MaskBackfillRunWorkflow,
)

__all__ = ["build_request", "main", "parse_args"]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.mask_backfill.run"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run", help="plan the backfill; run nothing")
    dry.add_argument("--tenant", required=True, help="the tenant's slug")
    dry.add_argument("--source", required=True, choices=["automatic", "headtail"])
    dry.add_argument("--missions", default="", help="comma-separated mission numbers")
    dry.add_argument("--parts", type=int, default=1, help="runs to split it into")
    dry.add_argument("--out", required=True, help="where to write the plan")
    apply = sub.add_parser("apply", help="run one part of a reviewed plan")
    apply.add_argument("--report", required=True, help="the reviewed plan")
    apply.add_argument("--part", type=int, default=0)
    return parser.parse_args(argv)


def build_request(args: argparse.Namespace) -> MaskBackfillRequest:
    if args.command == "apply":
        with open(args.report, encoding="utf-8") as handle:
            report = json.load(handle)
        if report.get("mode") != "dry_run":
            sys.exit(f"{args.report} is not a dry-run plan; refusing to apply it")
        parts = len(report["parts"])
        if not 0 <= args.part < parts:
            sys.exit(f"--part {args.part}: the plan has parts 0..{parts - 1}")
        return MaskBackfillRequest(
            tenant=report["tenant"], source=report["source"],
            numbers=[int(n) for n in report["numbers"]], apply=True,
            expected_plan_sha256=report["plan_sha256"], part=args.part, parts=parts,
        )  # fmt: skip
    if args.parts < 1:
        sys.exit("--parts is at least 1")
    numbers = [int(n) for n in args.missions.split(",") if n.strip()]
    return MaskBackfillRequest(tenant=args.tenant, source=args.source,
                               numbers=list(dict.fromkeys(numbers)), parts=args.parts)  # fmt: skip


def _summary(report: dict) -> str:
    missions = report["plan"]["missions"]
    lines = [f"{report['source']} masks owed: {sum(m['predictions'] for m in missions)} "
             f"predictions in {len(missions)} missions"]  # fmt: skip
    for i, part in enumerate(report["parts"]):
        owed = sum(m["predictions"] for m in missions if m["number"] in set(part))
        lines.append(f"  part {i}: {len(part)} missions, {owed} predictions")
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
        workflow_id = f"mask-backfill-{request.source}-apply-{request.part}-{stamp}"
        await client.start_workflow(MaskBackfillRunWorkflow.run, request,
                                    id=workflow_id, task_queue=temporal.task_queue)  # fmt: skip
        print(f"apply started: {workflow_id} (its result is the report)")
        return
    report = await client.execute_workflow(
        MaskBackfillRunWorkflow.run, request,
        id=f"mask-backfill-{request.source}-dry-run-{stamp}",
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
