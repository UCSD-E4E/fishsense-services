"""Every setting each production service requires is rendered or configured.

Each service's configuration is built exactly as the container would see it
-- its env_file renders (secrets.nix, with stand-in values) under its
`environment` -- and then handed to the service's own settings classes, which
validate at startup. So a missing or malformed FISHSENSE_* setting fails here,
not on the slot: v1 learnt that lesson as a crash-looping worker after a
converge (fishsense-lite deploy/README.md, "Settings-file changes ride the
deploy atomically").

The orchestrator goes further than its settings classes: it builds every
stage's activities, which is where each stage reads its own settings (the
registry, `Deps`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from _deploy import INCUS, REPO, services, service_env


@contextmanager
def _only(env: dict[str, str]) -> Iterator[None]:
    """The process environment as the container's: nothing of ours leaks in
    from the test runner's own FISHSENSE_* (a developer's shell)."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(("FISHSENSE_", "AUTH_", "LABEL_STUDIO", "PG")):
            del os.environ[key]
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _api(env):
    from fishsense_services_api.settings import Settings

    settings = Settings()
    assert settings.oidc_audiences


def _migrate(env):
    from fishsense_services_api.settings import MigrationSettings, V1MigrationSettings

    assert MigrationSettings().app_role == "fishsense_app"
    V1MigrationSettings()


def _orchestrator(env):
    from sqlalchemy.ext.asyncio import create_async_engine

    from fishsense_services_orchestrator.nrp.scaling import (
        NrpSettings,
        resolve_scaling_config,
    )
    from fishsense_services_orchestrator.registry import Deps
    from fishsense_services_orchestrator.settings import (
        OrchestratorSettings,
        TemporalSettings,
    )
    from fishsense_services_orchestrator.worker import build_activities

    # The image carries the manifests at /app/deploy/nrp (the Dockerfile); on
    # the test runner they are the repo's.
    os.environ["FISHSENSE_NRP_MANIFEST_DIR"] = str(REPO / "deploy" / "nrp")
    temporal = TemporalSettings()
    assert temporal.client_cert and temporal.domain == "workflows.krg.ucsd.edu"
    orchestrator = OrchestratorSettings()
    assert orchestrator.orchestrator_sub == "service:fishsense-orchestrator"
    engine = create_async_engine(orchestrator.database_url.get_secret_value())
    assert build_activities(Deps(engine=engine, sub=orchestrator.orchestrator_sub))
    assert resolve_scaling_config(NrpSettings()) is not None, "NRP stand-up is off"


def _backup(env):
    from fishsense_services_contracts.temporal import TemporalConnection
    from fishsense_services_orchestrator.ops.backup.settings import (
        BackupNasSettings,
        BackupSettings,
    )

    BackupSettings()
    BackupNasSettings()
    assert TemporalConnection().client_cert


def _cert_sync(env):
    from fishsense_services_orchestrator.ops.cert_sync import CertSyncSettings

    settings = CertSyncSettings.from_env()
    for name in (
        "kubeconfig_path",
        "namespace",
        "client_cert",
        "client_private_key",
        "server_root_ca_cert",
    ):
        assert getattr(settings, name), name


def _smoke(env):
    from fishsense_services_contracts.object_store import ObjectStoreConnection
    from fishsense_services_orchestrator.labels.label_studio import (
        LabelStudioSettings,
    )
    from fishsense_services_orchestrator.ops.smoke import SmokeSettings
    from fishsense_services_orchestrator.settings import TemporalSettings
    from fishsense_services_api.settings import MigrationSettings

    assert SmokeSettings().research_database_url is not None
    MigrationSettings()
    TemporalSettings()
    assert LabelStudioSettings().workspace
    ObjectStoreConnection()


def _web(env):
    """apps/web/lib/env.ts: the names its `env` proxy throws on."""
    source = (REPO / "apps" / "web" / "lib" / "env.ts").read_text()
    block = re.search(r"const ENV_VARS = \{(.*?)\}", source, re.S).group(1)
    required = set(re.findall(r'"([A-Z_]+)"', block)) | {"AUTH_URL"}
    assert required - set(env) == set()


def _superset(env):
    """What superset_config.py and docker-init.sh read."""
    for name in (
        "SUPERSET_SECRET_KEY",
        "DATABASE_PASSWORD",
        "AUTHENTIK_KEY",
        "AUTHENTIK_SECRET",
        "AUTHENTIK_ISSUER",
        "ANALYTICS_DATABASE_PASSWORD",
    ):
        assert env.get(name), name


def _postgres(env):
    assert env["POSTGRES_PASSWORD"]


def _bootstrap(env):
    """Every `${NAME:?...}` the script refuses to run without."""
    script = (INCUS / "db_bootstrap" / "bootstrap.sh").read_text()
    required = set(re.findall(r"\$\{([A-Z_]+):\?", script))
    assert required, "bootstrap.sh requires nothing?"
    assert required - set(env) == set()


def _nothing(env):
    """Configured entirely in the compose (traefik, valkey)."""


VALIDATORS: dict[str, Callable[[dict], None]] = {
    "api": _api,
    "migrate": _migrate,
    "orchestrator": _orchestrator,
    "backup": _backup,
    "nrp-temporal-cert-sync": _cert_sync,
    "smoke": _smoke,
    "web": _web,
    "superset": _superset,
    "superset-init": _superset,
    "superset-worker": _superset,
    "superset-worker-beat": _superset,
    "postgres": _postgres,
    "db-bootstrap": _bootstrap,
    "traefik": _nothing,
    "valkey": _nothing,
}


def test_every_service_is_checked():
    assert set(VALIDATORS) == set(services())


@pytest.mark.parametrize("name", sorted(VALIDATORS))
def test_the_service_starts_with_what_the_slot_gives_it(name):
    env = service_env(services()[name])
    if name.startswith("superset"):
        # The committed, non-secret half (v1's superset_volumes/docker/.env).
        committed = INCUS / "superset_volumes" / "docker" / ".env"
        for line in committed.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    with _only(env):
        VALIDATORS[name](env)


def test_the_stand_ins_really_are_what_the_checks_see():
    """Guards the harness: a render's value arrives, a missing one doesn't."""
    env = service_env(services()["api"])
    with _only(env):
        assert os.environ["FISHSENSE_DATABASE_URL"].startswith("postgresql+asyncpg://")
    with _only({}):
        with pytest.raises(Exception):
            _api({})
