"""The rehearsal stages the production compose without reaching production.

deploy/rehearsal/stage.py renders secrets.nix's templates itself (a small
consul-template) and layers compose.rehearsal.yml. What must hold: nothing
still points at /run/tenant or krg-prod's Temporal, the project is the
rehearsal's own, and the renders produce the values production's would --
including a DSN whose password needs escaping.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
import yaml
from sqlalchemy.engine import make_url

from _deploy import REPO, SECRETS_NIX, TENANT_RUN, renders

sys.path.insert(0, str(REPO / "deploy" / "rehearsal"))
import stage as sut  # noqa: E402

needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode,
    reason="needs the docker compose CLI (no daemon)",
)


def _env(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


# --- the renderer -------------------------------------------------------------------


def test_the_renderer_produces_every_variable_the_templates_name():
    rendered = sut.render_secrets(
        SECRETS_NIX.read_text(), {"nrp_orchestrator": {"kubeconfig": "k"}}
    )

    for destination, render in renders().items():
        if destination.endswith(".env"):
            assert set(_env(rendered[destination])) == set(
                render.variables
            ), destination


def test_urlquery_keeps_a_hostile_password_intact_in_the_dsn():
    """What `| urlquery` is for: `@`, `/`, `:` in a password would re-parse
    the URL. The DSN must decode back to the exact password."""
    hostile = "p@ss/w:rd+&%"
    rendered = sut.render_secrets(
        SECRETS_NIX.read_text(), {"services_db": {"owner_password": hostile}}
    )
    env = _env(rendered[f"{TENANT_RUN}/secrets/migrate.env"])

    url = make_url(env["FISHSENSE_MIGRATION_DATABASE_URL"])
    assert (url.username, url.password, url.host, url.database) == (
        "fishsense_owner",
        hostile,
        "postgres",
        "fishsense_services",
    )


def test_one_field_renders_the_same_everywhere():
    """The app role's password appears in api.env and orchestrator.env; a
    generated one must match in both, as one OpenBao field does."""
    rendered = sut.render_secrets(SECRETS_NIX.read_text(), {})
    api = _env(rendered[f"{TENANT_RUN}/secrets/api.env"])
    orchestrator = _env(rendered[f"{TENANT_RUN}/secrets/orchestrator.env"])
    bootstrap = _env(rendered[f"{TENANT_RUN}/secrets/db-bootstrap.env"])

    assert api["FISHSENSE_DATABASE_URL"] == orchestrator["FISHSENSE_DATABASE_URL"]
    assert make_url(api["FISHSENSE_DATABASE_URL"]).password == bootstrap["FISHSENSE_APP_PASSWORD"]  # fmt: skip


def test_an_unseeded_soft_render_is_empty():
    rendered = sut.render_secrets(SECRETS_NIX.read_text(), {})
    assert rendered[f"{TENANT_RUN}/nrp/kubeconfig"] == ""


# --- the staged stack ----------------------------------------------------------------


def _config(dc) -> dict:
    result = subprocess.run(
        [str(dc), "config", "--format", "yaml"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)


@needs_compose
def test_the_staged_stack_is_the_rehearsals_own(tmp_path):
    dc = sut.stage(tmp_path / "rehearsal", local_images=True)

    model = _config(dc)
    text = yaml.safe_dump(model)

    assert model["name"] == "fishsense-rehearsal"
    assert model["volumes"]["pgdata"]["name"] == "fishsense-rehearsal_pgdata"
    assert TENANT_RUN not in text
    assert "krg-prod" not in text
    assert "ghcr.io" not in text
    assert "traefik" not in model["services"]
    for name in ("orchestrator", "backup"):
        env = model["services"][name]["environment"]
        assert env["FISHSENSE_TEMPORAL_ADDRESS"] == "temporal:7233"
        assert "FISHSENSE_TEMPORAL_CLIENT_CERT" not in env
    assert "FISHSENSE_NRP_KUBECONFIG_PATH" not in model["services"]["orchestrator"]["environment"]  # fmt: skip
    assert {"processor-light", "processor-per-image", "temporal"} <= set(
        model["services"]
    )


@needs_compose
def test_a_release_can_be_rehearsed_as_released(tmp_path):
    dc = sut.stage(tmp_path / "rehearsal", version="v9.8.7")

    images = {s["image"] for s in _config(dc)["services"].values()}

    assert "ghcr.io/ucsd-e4e/fishsense-services-api:v9.8.7" in images
    assert "ghcr.io/ucsd-e4e/fishsense-services-processor:v9.8.7" in images


def test_the_stage_leaves_production_files_alone(tmp_path):
    before = (REPO / "deploy" / "incus" / "compose.yml").read_text()
    sut.stage(tmp_path / "rehearsal", version="v9.8.7")
    assert (REPO / "deploy" / "incus" / "compose.yml").read_text() == before
