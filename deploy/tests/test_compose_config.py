"""`docker compose config` accepts the production compose, as the slot runs it.

compose itself is the only complete validator of its file: env_file paths,
depends_on conditions, profiles, anchors. It refuses a missing env_file, so the
renders are stood in for in a temporary directory: the compose is copied with
/run/tenant rewritten there, and each render file written with its variable
names (and each relative bind created) as vault-agent and workdir.nix would.
`config` parses only; it needs no daemon and starts nothing.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from _deploy import COMPOSE, INCUS, TENANT_RUN, renders, rendered_value

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode,
    reason="needs the docker compose CLI (no daemon)",
)


def _stage(tmp_path, profiles: str = ""):
    run = tmp_path / "run-tenant"
    for destination, render in renders().items():
        path = run / destination.removeprefix(TENANT_RUN + "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(f"{k}={rendered_value(v)}\n" for k, v in render.variables.items())
        )
    for sub in ("tls", "temporal"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    shutil.copytree(INCUS, project, ignore=shutil.ignore_patterns("compose.yml"))
    (project / ".env").write_text(f"COMPOSE_PROFILES={profiles}\n")
    # As on the slot: the compose file is in the store, not the project dir.
    store = tmp_path / "store"
    store.mkdir()
    compose = store / "compose.yml"
    compose.write_text(COMPOSE.read_text().replace(TENANT_RUN, str(run)))
    return project, compose


def _config(project, compose, *extra):
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(project),
            "-f",
            str(compose),
            *extra,
            "config",
            "--format",
            "yaml",
        ],
        capture_output=True,
        text=True,
    )


def test_compose_accepts_the_production_file(tmp_path):
    project, compose = _stage(tmp_path)

    result = _config(project, compose)

    assert result.returncode == 0, result.stderr
    model = yaml.safe_load(result.stdout)
    assert model["name"] == "project"  # the slot's is the working dir's: fishsense
    # Off by default: superset and the smoke test are not in the converge.
    assert "superset" not in model["services"]
    assert "smoke" not in model["services"]
    assert {"postgres", "db-bootstrap", "migrate", "api", "web", "orchestrator"} <= set(
        model["services"]
    )


def test_compose_accepts_it_with_superset_on(tmp_path):
    project, compose = _stage(tmp_path, profiles="superset")

    result = _config(project, compose)

    assert result.returncode == 0, result.stderr
    services = yaml.safe_load(result.stdout)["services"]
    assert {"superset", "superset-init", "superset-worker", "valkey"} <= set(services)


def test_the_project_name_on_the_slot_is_v1s(tmp_path):
    """The volume `pgdata` is v1's `fishsense_pgdata` only if the project is
    named `fishsense`, which the composeStack gets from its working directory
    (/var/lib/krg/fishsense)."""
    project, compose = _stage(tmp_path)
    slot = tmp_path / "fishsense"
    project.rename(slot)

    result = _config(slot, compose)

    assert result.returncode == 0, result.stderr
    model = yaml.safe_load(result.stdout)
    assert model["volumes"]["pgdata"]["name"] == "fishsense_pgdata"
    assert model["networks"]["interior"]["name"] == "fishsense_interior"
