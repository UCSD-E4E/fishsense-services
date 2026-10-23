"""Operator CLI: calibration v3's read-only replay (2026-10-22). Run where the
orchestrator's Temporal settings are::

    python -m fishsense_services_orchestrator.calibration_v3.replay start \\
        --tenant lab [--missions 62,58] [--draw-missions 59,60] \\
        [--sizes 3,4,5,6,8] [--draws 20]

    python -m fishsense_services_orchestrator.calibration_v3.replay collect \\
        --workflow-id calibration-v3-replay-... --out /tmp/v3.json

`start` writes nothing anywhere; `collect` gathers each mission's report from
its child workflow into one file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from fishsense_services_orchestrator.calibration_v3.workflow import (
    ReplayCalibrationV3Workflow,
    ReplayRequest,
)

__all__ = ["main", "parse_args"]


def _ints(text: str) -> list[int]:
    return [int(n) for n in text.split(",") if n.strip()]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.calibration_v3.replay"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="replay v3, read-only")
    start.add_argument("--tenant", required=True)
    start.add_argument("--missions", default="")
    start.add_argument("--draw-missions", default="")
    start.add_argument("--sizes", default="3,4,5,6,8")
    start.add_argument("--draws", type=int, default=20)
    start.add_argument("--size-source", choices=["sift", "sam", "pose"], default="sift")
    collect = sub.add_parser("collect", help="gather the reports")
    collect.add_argument("--workflow-id", required=True)
    collect.add_argument("--out", required=True)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    # pylint: disable=import-outside-toplevel
    from temporalio.client import Client

    from fishsense_services_contracts.temporal import connect_options
    from fishsense_services_orchestrator.settings import TemporalSettings

    temporal = TemporalSettings()
    client = await Client.connect(**connect_options(temporal))
    if args.command == "start":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        workflow_id = f"calibration-v3-replay-{stamp}"
        request = ReplayRequest(tenant=args.tenant, numbers=_ints(args.missions),
                                draw_numbers=_ints(args.draw_missions),
                                draw_sizes=_ints(args.sizes), draws_per_size=args.draws,
                                size_source=args.size_source)  # fmt: skip
        await client.start_workflow(ReplayCalibrationV3Workflow.run, request,
                                    id=workflow_id, task_queue=temporal.task_queue)  # fmt: skip
        print(f"replay started: {workflow_id}")
        return
    summary = await client.get_workflow_handle(args.workflow_id).result()
    reports = []
    for row in summary["missions"]:
        entry = dict(row)
        if "error" not in row:
            entry["report"] = await client.get_workflow_handle(row["child"]).result()
        reports.append(entry)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"tenant_id": summary["tenant_id"], "missions": reports}, handle,
                  indent=1, default=str)  # fmt: skip
    print(f"{len(reports)} missions -> {args.out}")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(_run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
