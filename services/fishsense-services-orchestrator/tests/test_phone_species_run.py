"""The phone species' operator CLI: a dry run by default, told its region;
apply only a reviewed dry-run plan, by its digest, in the plan's region."""

from __future__ import annotations

import json

import pytest

from fishsense_services_orchestrator.phone_species.run import build_request, parse_args

SOCAL = "Southern California, USA"


def test_a_dry_run_asks_for_the_region_and_the_missions_given_once_each():
    request = build_request(parse_args(["dry-run", "--tenant", "ccfrp-ucsd",
                                        "--region", SOCAL, "--missions", "3,4,3",
                                        "--out", "/tmp/x"]))  # fmt: skip
    assert (request.tenant, request.region, request.numbers, request.apply) == (
        "ccfrp-ucsd",
        SOCAL,
        [3, 4],
        False,
    )


def test_a_dry_run_needs_a_region():
    with pytest.raises(SystemExit):
        parse_args(["dry-run", "--tenant", "ccfrp-ucsd", "--out", "/tmp/x"])


def test_apply_runs_the_reviewed_plan_in_its_region_by_its_digest(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"mode": "dry_run", "tenant": "ccfrp-ucsd",
                                "region": SOCAL, "numbers": [3],
                                "plan_sha256": "abc"}))  # fmt: skip
    request = build_request(parse_args(["apply", "--report", str(plan)]))
    assert (request.tenant, request.region, request.numbers) == (
        "ccfrp-ucsd",
        SOCAL,
        [3],
    )
    assert request.apply and request.expected_plan_sha256 == "abc"


def test_apply_refuses_anything_but_a_dry_run_plan(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"mode": "apply", "tenant": "ccfrp-ucsd",
                                  "region": SOCAL, "numbers": [],
                                  "plan_sha256": "abc"}))  # fmt: skip
    with pytest.raises(SystemExit):
        build_request(parse_args(["apply", "--report", str(report)]))
