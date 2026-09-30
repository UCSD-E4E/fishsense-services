"""build -> release -> promote -> deploy, and the flake the slot converges.

Mirrors fishsense-lite's pipeline (.github/workflows/{build,release,promote,
deploy}.yml, release-please-config.json) with one release version for the
whole monorepo. What is pinned here is what would otherwise drift silently:
a new image CI builds but promote never retags, a pin promote's bump misses,
the processor tag left behind by a release.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import yaml

from _deploy import (
    COMPOSE,
    FLAKE_LOCK,
    IMAGES,
    REPO,
    WORKFLOWS,
    environment,
    flake_attr,
    image_version,
    services,
)

BUMP = REPO / "deploy" / "bump_pins.py"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _matrix_images(workflow: str) -> set[str]:
    jobs = _workflow(workflow)["jobs"]
    (job,) = [j for j in jobs.values() if "strategy" in j]
    return {entry["image"] for entry in job["strategy"]["matrix"]["include"]}


# --- promote's bump -------------------------------------------------------------


def _bump(tmp_path, version: str) -> str:
    copy = tmp_path / "compose.yml"
    copy.write_text(COMPOSE.read_text())
    subprocess.run([sys.executable, str(BUMP), version, str(copy)], check=True)
    return copy.read_text()


def test_the_bump_moves_every_pin_and_the_processor_tag(tmp_path):
    bumped = yaml.safe_load(_bump(tmp_path, "v98.76.54"))["services"]

    for name, service in bumped.items():
        if image_version(service["image"]):
            assert image_version(service["image"]) == "v98.76.54", name
    assert environment(bumped["orchestrator"])["FISHSENSE_NRP_IMAGE_TAG"] == "v98.76.54"


def test_the_bump_changes_nothing_else(tmp_path):
    before = COMPOSE.read_text().splitlines()
    after = _bump(tmp_path, "v98.76.54").splitlines()

    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after)
    assert changed, "the bump changed nothing"
    for old, new in changed:
        assert re.sub(r"v\d+\.\d+\.\d+", "V", old) == re.sub(
            r"v\d+\.\d+\.\d+", "V", new
        )


def test_the_bump_refuses_a_version_that_is_not_one(tmp_path):
    copy = tmp_path / "compose.yml"
    copy.write_text(COMPOSE.read_text())
    result = subprocess.run(
        [sys.executable, str(BUMP), "latest", str(copy)], capture_output=True
    )
    assert result.returncode != 0
    assert copy.read_text() == COMPOSE.read_text()


def test_the_bump_refuses_a_compose_with_nothing_to_bump(tmp_path):
    """A parser that matches nothing would open an empty PR, or none, and the
    release would never deploy -- loudly, not silently."""
    copy = tmp_path / "compose.yml"
    copy.write_text("services: {}\n")
    result = subprocess.run([sys.executable, str(BUMP), "v1.0.0", str(copy)])
    assert result.returncode != 0


def test_promote_bumps_with_the_script_the_tests_exercise():
    text = (WORKFLOWS / "promote.yml").read_text()
    assert "python3 deploy/bump_pins.py" in text
    assert "deploy/incus/compose.yml" in text


# --- one image list, everywhere ----------------------------------------------------


def test_build_promote_and_rebuild_agree_on_the_six_images():
    assert _matrix_images("build.yml") == set(IMAGES)
    assert _matrix_images("rebuild-from-main.yml") == set(IMAGES)
    assert set(_workflow("promote.yml")["env"]["IMAGES"].split()) == set(IMAGES)


def test_every_image_is_built_from_a_real_target():
    """build.yml's `target` is a Dockerfile stage (or the web's own file)."""
    stages = set(
        re.findall(r"^FROM \S+ AS (\S+)", (REPO / "Dockerfile").read_text(), re.M)
    )
    jobs = _workflow("build.yml")["jobs"]
    (job,) = [j for j in jobs.values() if "strategy" in j]
    for entry in job["strategy"]["matrix"]["include"]:
        assert (REPO / entry["dockerfile"]).is_file(), entry
        if entry["dockerfile"] == "Dockerfile":
            assert entry["target"] in stages, entry


def test_prs_build_but_never_push():
    text = (WORKFLOWS / "build.yml").read_text()
    assert "push: ${{ github.event_name == 'push' }}" in text
    on = _workflow("build.yml")[True]  # YAML 1.1 reads `on` as True
    assert on["push"]["branches"] == ["main"] and "pull_request" in on


# --- one release -----------------------------------------------------------------------


def test_release_please_cuts_one_version_for_the_monorepo():
    config = json.loads((REPO / "release-please-config.json").read_text())
    manifest = json.loads((REPO / ".release-please-manifest.json").read_text())

    assert list(config["packages"]) == ["."]
    assert list(manifest) == ["."]
    assert config.get("include-component-in-tag") is False


def test_the_compose_pins_are_a_released_or_the_bootstrap_version():
    """Before the first release the pins name the manifest's bootstrap version;
    after it, promote keeps them at most one release behind the manifest."""
    manifest = json.loads((REPO / ".release-please-manifest.json").read_text())["."]
    pin = environment(services()["orchestrator"])["FISHSENSE_NRP_IMAGE_TAG"]
    assert tuple(map(int, pin[1:].split("."))) <= tuple(map(int, manifest.split(".")))


def test_deploy_converges_only_on_a_merged_auto_deploy_pr_or_by_hand():
    """v1's gate (fishsense-lite deploy.yml): a human reviews the pin diff. A
    push trigger would converge on the branch push that opens the PR."""
    workflow = _workflow("deploy.yml")
    on = workflow[True]
    assert set(on) == {"pull_request", "workflow_dispatch"}
    job = workflow["jobs"]["deploy-incus"]
    assert job["runs-on"] == ["self-hosted", "fishsense"]
    assert "auto-deploy/" in job["if"]
    assert "systemctl start --no-block fishsense-selfupdate" in json.dumps(job)


# --- the flake ------------------------------------------------------------------------


def test_the_tenant_is_v1s_slot():
    """Decision 1: same name, zone, host, resources, image and SSO group, so
    the admin's only boundary change is the runner scope and the selfupdate
    target."""
    assert flake_attr("name") == '"fishsense"'
    assert flake_attr("zone") == '"e4e"'
    assert flake_attr("hostname") == '"fishsense.e4e.ucsd.edu"'
    assert flake_attr("sso.group") == '"FishSense"'
    assert flake_attr("cpu") == "6"
    assert flake_attr("ram") == '"12GiB"'
    assert flake_attr("image") == '"krg-golden"'
    assert flake_attr("repo") == '"UCSD-E4E/fishsense-services"'
    assert flake_attr("compose") == "./deploy/incus/compose.yml"
    assert flake_attr("namespace") == '"fishsense"'


def test_the_flake_imports_the_interior_modules():
    text = (REPO / "flake.nix").read_text()
    for module in ("secrets.nix", "workdir.nix", "prune.nix", "cert-sync-timer.nix"):
        assert f"./deploy/incus/{module}" in text, module


def test_the_lock_pins_krg_infra_and_nixpkgs_follows_it():
    lock = json.loads(FLAKE_LOCK.read_text())
    nodes = lock["nodes"]
    krg = nodes["krg-infra"]
    assert krg["original"] == {
        "dir": "nix",
        "owner": "KastnerRG",
        "repo": "krg-infra",
        "type": "github",
    }
    assert re.fullmatch(r"[0-9a-f]{40}", krg["locked"]["rev"])
    assert nodes["root"]["inputs"]["nixpkgs"] == ["krg-infra", "nixpkgs"]


def test_the_weekly_flake_bump_evaluates_this_tenant():
    text = (WORKFLOWS / "update-flake.yml").read_text()
    assert "nix flake update krg-infra" in text
    assert (
        ".#nixosConfigurations.fishsense.config.system.build.toplevel.drvPath" in text
    )
