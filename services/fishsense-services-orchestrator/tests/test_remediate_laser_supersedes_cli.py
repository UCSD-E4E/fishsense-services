"""The operator CLI for laser-supersede remediation.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_remediate_laser_supersedes_cli.py. What it must guarantee, because
it is the thing a person runs against prod:

* no arguments means a dry run;
* apply takes EVERYTHING from the reviewed report -- its dives, its exclusions
  and its digest -- so what is written is what was reviewed, and nothing typed
  on the command line at apply time can widen it;
* apply cannot be reached without naming a report.

v2 change: dives and labels are named by number (v1's ids for migrated rows),
and "every dive" is the workflow's to resolve (`all_missions`) -- the v2 CLI runs
with Temporal settings only, no database.
"""

from __future__ import annotations

import json

import pytest

from fishsense_services_orchestrator.laser import remediate as cli


def test_no_flags_is_a_dry_run_over_the_named_missions():
    args = cli.parse_args(["dry-run", "--dives", "7,9", "--out", "r.json"])

    request = cli.build_request(args)

    assert request.apply is False
    assert request.mission_ids == [7, 9] and request.all_missions is False
    assert request.expected_plan_sha256 is None


def test_without_missions_it_plans_every_mission():
    args = cli.parse_args(["dry-run", "--out", "r.json"])

    request = cli.build_request(args)

    assert request.all_missions is True and request.mission_ids == []


def test_exclusions_come_from_a_file(tmp_path):
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(json.dumps({"mission_ids": [77], "label_ids": [5, 6]}))
    args = cli.parse_args(
        ["dry-run", "--out", "r.json", "--exclusions", str(exclusions)]
    )

    request = cli.build_request(args)

    assert request.excluded_mission_ids == [77]
    assert request.excluded_label_ids == [5, 6]


def test_an_exclusions_file_from_before_the_rename_still_excludes(tmp_path):
    """A file reviewed before Phase 2a names its missions `dive_ids`: dropped,
    those missions would be remediated after all."""
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(json.dumps({"dive_ids": [77], "label_ids": [5]}))
    args = cli.parse_args(
        ["dry-run", "--out", "r.json", "--exclusions", str(exclusions)]
    )

    request = cli.build_request(args)

    assert request.excluded_mission_ids == [77]


def test_the_old_flags_still_work():
    assert (
        cli.parse_args(["dry-run", "--out", "r.json", "--dives", "3,4"]).missions
        == "3,4"
    )


def test_apply_takes_everything_from_the_reviewed_report(tmp_path):
    report = {
        "mode": "dry_run",
        "plan_sha256": "f" * 64,
        "excluded_mission_ids": [77],
        "excluded_label_ids": [5],
        "missions": [{"mission_id": 7}, {"mission_id": 77}],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    args = cli.parse_args(["apply", "--report", str(path)])

    request = cli.build_request(args)

    assert request.apply is True and request.all_missions is False
    assert request.expected_plan_sha256 == "f" * 64
    assert request.mission_ids == [7, 77]
    assert request.excluded_mission_ids == [77]
    assert request.excluded_label_ids == [5]


def test_apply_needs_a_report():
    with pytest.raises(SystemExit):
        cli.parse_args(["apply"])


def test_apply_refuses_a_report_that_is_not_a_dry_run(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps({"mode": "apply", "plan_sha256": "f" * 64, "missions": []})
    )
    args = cli.parse_args(["apply", "--report", str(path)])

    with pytest.raises(SystemExit):
        cli.build_request(args)


def test_the_report_is_written_as_json(tmp_path):
    out = tmp_path / "r.json"
    cli.write_report({"mode": "dry_run", "missions": []}, str(out))

    assert json.loads(out.read_text())["mode"] == "dry_run"


def test_it_starts_the_parent_on_the_orchestrators_queue():
    from fishsense_services_contracts.laser import RemediateLaserSupersedesInput
    from fishsense_services_orchestrator.laser.workflow import (
        RemediateLaserSupersedesParentWorkflow,
    )

    assert cli.PARENT_WORKFLOW == "RemediateLaserSupersedesParentWorkflow"
    assert (
        RemediateLaserSupersedesParentWorkflow.__temporal_workflow_definition.name
        == cli.PARENT_WORKFLOW
    )
    assert RemediateLaserSupersedesInput(mission_ids=[]).apply is False


def test_a_report_reviewed_before_the_rename_still_applies_with_its_exclusions(
    tmp_path,
):
    """Its rows say `dives` / `dive_id` and its exclusions `excluded_dive_ids`;
    dropped, a reviewed exclusion would lapse."""
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "mode": "dry_run", "plan_sha256": "f" * 64,
        "dives": [{"dive_id": 3}, {"dive_id": 4}],
        "excluded_dive_ids": [9], "excluded_label_ids": [5],
    }))  # fmt: skip

    request = cli.build_request(cli.parse_args(["apply", "--report", str(report)]))

    assert request.mission_ids == [3, 4]
    assert request.excluded_mission_ids == [9]
    assert request.excluded_label_ids == [5]
