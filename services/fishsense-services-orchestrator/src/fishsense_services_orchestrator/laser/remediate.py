"""Operator CLI: revive laser labels the eroding validator superseded.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
src/fishsense_api_workflow_worker/remediate_laser_supersedes.py (#932). Run
where the orchestrator's Temporal settings are (``FISHSENSE_TEMPORAL_*``):

    python -m fishsense_services_orchestrator.laser.remediate \\
        dry-run --out /tmp/report.json [--dives 7,9] [--exclusions excl.json]

    python -m fishsense_services_orchestrator.laser.remediate \\
        apply --report /tmp/report.json

`dry-run` writes nothing and produces the report. `apply` takes its dives,
exclusions and plan digest from the reviewed report and nothing else; the
workflow re-plans every dive and refuses unless the digest still matches, and
each dive's apply refuses any id its fresh plan does not contain.
`excl.json` is ``{"dive_ids": [...], "label_ids": [...]}``.

v2 changes: dives and labels are named by number (v1's ids for migrated rows,
so a v1 exclusion file still reads); "every dive" is resolved by the workflow
(`all_missions`), since this CLI holds no database connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from fishsense_services_contracts.laser import RemediateLaserSupersedesInput

__all__ = ["PARENT_WORKFLOW", "build_request", "main", "parse_args", "write_report"]

PARENT_WORKFLOW = "RemediateLaserSupersedesParentWorkflow"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """`dry-run` or `apply --report`."""
    parser = argparse.ArgumentParser(
        prog="fishsense_services_orchestrator.laser.remediate"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    dry = sub.add_parser("dry-run", help="plan every dive; write nothing")
    dry.add_argument("--out", required=True, help="where to write the report")
    dry.add_argument(
        "--missions", "--dives", help="comma-separated dive numbers (default: all)"
    )
    dry.add_argument(
        "--exclusions",
        help='JSON {"mission_ids": [], "label_ids": []} ("dive_ids" still read)',
    )
    apply = sub.add_parser("apply", help="apply a reviewed dry-run report")
    apply.add_argument("--report", required=True, help="the reviewed report")
    apply.add_argument("--out", help="where to write the apply report")
    return parser.parse_args(argv)


def with_current_names(report: dict) -> dict:
    """A dry-run report under today's names. One reviewed before Phase 2a
    names its rows `dives` and each one's id `dive_id`, and its exclusions
    `excluded_dive_ids` -- dropped, a reviewed exclusion would lapse."""
    report = dict(report)
    if "missions" not in report and "dives" in report:
        report["missions"] = report.pop("dives")
    report["missions"] = [
        {
            ("mission_id" if key == "dive_id" else key): value
            for key, value in row.items()
        }
        for row in report.get("missions", [])
    ]
    if "excluded_mission_ids" not in report and "excluded_dive_ids" in report:
        report["excluded_mission_ids"] = report.pop("excluded_dive_ids")
    return report


def build_request(args: argparse.Namespace) -> RemediateLaserSupersedesInput:
    """The workflow input for these arguments."""
    if args.command == "apply":
        with open(args.report, encoding="utf-8") as handle:
            report = with_current_names(json.load(handle))
        if report.get("mode") != "dry_run":
            sys.exit(f"{args.report} is not a dry-run report; refusing to apply it")
        return RemediateLaserSupersedesInput(
            mission_ids=sorted(row["mission_id"] for row in report["missions"]),
            excluded_mission_ids=list(report.get("excluded_mission_ids", [])),
            excluded_label_ids=list(report.get("excluded_label_ids", [])),
            apply=True,
            expected_plan_sha256=report["plan_sha256"],
        )

    exclusions = {"mission_ids": [], "label_ids": []}
    if args.exclusions:
        with open(args.exclusions, encoding="utf-8") as handle:
            loaded = json.load(handle)
        # A file reviewed before Phase 2a names them `dive_ids`.
        if "dive_ids" in loaded:
            loaded.setdefault("mission_ids", loaded.pop("dive_ids"))
        exclusions.update(loaded)
    mission_ids = (
        sorted(int(d) for d in args.missions.split(",") if d.strip())
        if args.missions
        else []
    )
    return RemediateLaserSupersedesInput(
        mission_ids=mission_ids,
        all_missions=not args.missions,
        excluded_mission_ids=[int(d) for d in exclusions["mission_ids"]],
        excluded_label_ids=[int(i) for i in exclusions["label_ids"]],
    )


def write_report(report: dict, path: str) -> None:
    """The report as indented JSON."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


async def _run(args: argparse.Namespace) -> None:
    # pylint: disable=import-outside-toplevel
    from temporalio.client import Client

    from fishsense_services_contracts.temporal import connect_options
    from fishsense_services_orchestrator.settings import TemporalSettings

    request = build_request(args)
    temporal = TemporalSettings()
    client = await Client.connect(**connect_options(temporal))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = await client.execute_workflow(
        PARENT_WORKFLOW,
        request,
        id=f"remediate-laser-supersedes-{args.command}-{stamp}",
        task_queue=temporal.task_queue,
        result_type=dict,
    )
    out = args.out or f"{args.report}.applied.json"
    write_report(report, out)
    totals = report["totals"]
    print(
        f"{report['mode']}: {totals['to_revive']} to revive across "
        f"{totals['missions']} dives; digest {report['plan_sha256']}; report -> {out}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(_run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    main()
