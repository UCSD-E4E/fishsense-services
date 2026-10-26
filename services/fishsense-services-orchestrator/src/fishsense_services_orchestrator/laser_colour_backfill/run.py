"""Operator CLI: the laser-colour backfill (2026-10-26). A dry run unless
``--apply``. Run where the orchestrator's Temporal settings are::

    python -m fishsense_services_orchestrator.laser_colour_backfill.run start \\
        --tenant lab [--missions 62,58] [--apply]

    python -m fishsense_services_orchestrator.laser_colour_backfill.run report \\
        --workflow-id laser-colour-backfill-dry-...

`start` prints the run's id; `report` waits for it and prints its counts as
JSON: each mission's, and the totals -- readings by colour, the frames left
without one by why, the comparisons with the labellers' colours
(``vs_label``) and the live kernel's (``vs_kernel``) as
``"<reading>/<theirs>"``, and how many readings were written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from fishsense_services_orchestrator.laser_colour_backfill.workflow import (
    BackfillLaserColoursWorkflow,
    ColourBackfillRequest,
)

__all__ = ["main", "parse_args", "workflow_id"]


def _ints(text: str) -> list[int]:
    return [int(n) for n in text.split(",") if n.strip()]


def workflow_id(apply: bool, now: datetime) -> str:
    """``laser-colour-backfill-dry-…`` or ``-apply-…``: the id says which."""
    mode = "apply" if apply else "dry"
    return f"laser-colour-backfill-{mode}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.laser_colour_backfill.run"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="read the colours (a dry run unless --apply)")
    start.add_argument("--tenant", required=True)
    start.add_argument("--missions", default="")
    start.add_argument("--apply", action="store_true",
                       help="write the readings still owed")  # fmt: skip
    report = sub.add_parser("report", help="print a run's counts")
    report.add_argument("--workflow-id", required=True)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    # pylint: disable=import-outside-toplevel
    from temporalio.client import Client

    from fishsense_services_contracts.temporal import connect_options
    from fishsense_services_orchestrator.settings import TemporalSettings

    temporal = TemporalSettings()
    client = await Client.connect(**connect_options(temporal))
    if args.command == "start":
        run_id = workflow_id(args.apply, datetime.now(timezone.utc))
        await client.start_workflow(
            BackfillLaserColoursWorkflow.run,
            ColourBackfillRequest(tenant=args.tenant, numbers=_ints(args.missions),
                                  apply=args.apply),
            id=run_id, task_queue=temporal.task_queue,
        )  # fmt: skip
        print(f"{'APPLY' if args.apply else 'DRY RUN'} started: {run_id}")
        return
    report = await client.get_workflow_handle(args.workflow_id).result()
    print(json.dumps(report, indent=1, default=str))


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(_run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
