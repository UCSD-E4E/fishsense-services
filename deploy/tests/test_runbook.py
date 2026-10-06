"""docs/cutover.md's lists are the code's.

The runbook pauses, deletes and unpauses schedules by id from two shell
lists. A list that drifts from the code pauses the wrong thing on the night --
or misses one, which then fires into the cutover.
"""

from __future__ import annotations

import re

from _deploy import REPO

RUNBOOK = (REPO / "docs" / "cutover.md").read_text()


def _shell_list(name: str) -> set[str]:
    match = re.search(rf'^{name}="([^"]*)"', RUNBOOK, re.M)
    assert match, f"docs/cutover.md defines no {name}"
    return set(match.group(1).split())


def test_the_runbooks_v2_schedules_are_the_ones_v2_ensures(monkeypatch):
    """With production's settings: BioCLIP's schedule is off (compose.yml)."""
    monkeypatch.setenv("FISHSENSE_SPECIES_PREDICTION_ENABLED", "false")
    from fishsense_services_orchestrator.ops.smoke import expected_schedule_ids

    assert _shell_list("V2_SCHEDULES") == expected_schedule_ids()


def test_the_runbooks_v1_schedules_are_the_ones_the_smoke_test_forbids():
    from fishsense_services_orchestrator.ops.smoke import V1_SCHEDULE_IDS

    listed = {f"{s}-workflow-schedule" for s in _shell_list("V1_SCHEDULES")}
    assert listed | {"fishsense-daily-db-backup"} == V1_SCHEDULE_IDS


def test_the_runbook_names_every_new_openbao_path():
    secrets = (REPO / "deploy" / "incus" / "secrets.nix").read_text()
    paths = set(re.findall(r"secret/data/tenants/fishsense/([a-z_/]+)", secrets))
    for path in paths:
        assert f"`{path}`" in RUNBOOK, path


def test_production_turns_bioclip_off_as_the_runbook_assumes():
    import yaml

    compose = yaml.safe_load((REPO / "deploy" / "incus" / "compose.yml").read_text())
    env = compose["services"]["orchestrator"]["environment"]
    assert env["FISHSENSE_SPECIES_PREDICTION_ENABLED"] == "false"


def test_production_turns_the_slate_detector_off_until_its_weights_are_pinned():
    """Its schedule needs the processor's FISHSENSE_SLATE_DETECTOR_* pin and
    the weights in model-weights; turning it on is a reviewed diff."""
    import yaml

    compose = yaml.safe_load((REPO / "deploy" / "incus" / "compose.yml").read_text())
    env = compose["services"]["orchestrator"]["environment"]
    assert env["FISHSENSE_SLATE_DETECTION_ENABLED"] == "false"
