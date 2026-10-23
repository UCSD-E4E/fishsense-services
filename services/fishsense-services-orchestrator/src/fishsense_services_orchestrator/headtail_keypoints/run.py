"""Operator CLI: the SimCC head/tail keypoint replay, read-only (2026-10-23).
Run where the orchestrator's Temporal settings are::

    python -m fishsense_services_orchestrator.headtail_keypoints.run start \\
        --tenant lab [--missions 62,58]

    python -m fishsense_services_orchestrator.headtail_keypoints.run collect \\
        --workflow-id headtail-keypoints-... --out /tmp/keypoints.json

`start` writes nothing anywhere; `collect` gathers each mission's results from
its child workflow into one file.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from fishsense_services_orchestrator.headtail_keypoints.workflow import (
    KeypointReplayRequest,
    ReplayHeadtailKeypointsWorkflow,
)

__all__ = ["main", "parse_args"]


def _ints(text: str) -> list[int]:
    return [int(n) for n in text.split(",") if n.strip()]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.headtail_keypoints.run"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="replay the keypoint model, read-only")
    start.add_argument("--tenant", required=True)
    start.add_argument("--missions", default="")
    collect = sub.add_parser("collect", help="gather the results")
    collect.add_argument("--workflow-id", required=True)
    collect.add_argument("--out", required=True)
    return parser.parse_args(argv)


def _plain(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


async def _run(args: argparse.Namespace) -> None:
    # pylint: disable=import-outside-toplevel
    from temporalio.client import Client

    from fishsense_services_contracts.temporal import connect_options
    from fishsense_services_orchestrator.settings import TemporalSettings

    temporal = TemporalSettings()
    client = await Client.connect(**connect_options(temporal))
    if args.command == "start":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        workflow_id = f"headtail-keypoints-{stamp}"
        await client.start_workflow(
            ReplayHeadtailKeypointsWorkflow.run,
            KeypointReplayRequest(tenant=args.tenant, numbers=_ints(args.missions)),
            id=workflow_id, task_queue=temporal.task_queue,
        )  # fmt: skip
        print(f"replay started: {workflow_id}")
        return
    summary = await client.get_workflow_handle(args.workflow_id).result()
    missions = []
    for row in summary["missions"]:
        entry = dict(row)
        if "error" not in row:
            entry["result"] = _plain(
                await client.get_workflow_handle(row["child"]).result()
            )
        missions.append(entry)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"tenant_id": summary["tenant_id"], "missions": missions}, handle,
                  default=str)  # fmt: skip
    print(f"{len(missions)} missions -> {args.out}")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(_run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
