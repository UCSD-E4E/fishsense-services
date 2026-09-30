"""The production compose (deploy/incus/compose.yml): the invariants the slot
depends on and nothing else would catch before a converge.

Each test names the failure it prevents. Most are ones v1 had in production
(fishsense-lite deploy/incus/*: its comments cite the outages), a few are v2's
own credential rules (PLAN.md §9.10: the API never holds the owner's DSN, and
the orchestrator never the backup's).
"""

from __future__ import annotations

import re

import pytest

from _deploy import (
    GHCR,
    IMAGES,
    INCUS,
    REPO,
    TENANT_RUN,
    VERSION,
    WORKDIR_NIX,
    compose,
    converged_services,
    env_files,
    environment,
    image_version,
    mounts,
    reload_list,
    renders,
    service_env,
    services,
    volumes,
)

V2_DATABASE = "fishsense_services"
V1_DATABASE = "fishsense"


# --- images ---------------------------------------------------------------------


def test_nothing_is_built_on_the_slot():
    """The slot runs released images; a `build:` would compile on a 6-vCPU box
    with a 20 GB disk, from whatever the flake checked out."""
    assert [n for n, s in services().items() if "build" in s] == []


def test_every_image_is_pinned():
    """A floating tag redeploys whatever was pushed last, on any recreate --
    and the composeStack force-recreates the whole stack on every changed
    converge (krg-infra #459)."""
    for name, service in services().items():
        image = service["image"]
        repo, _, tag = image.rpartition(":")
        assert repo and tag and "/" not in tag, f"{name}: {image} has no tag"
        assert tag != "latest", f"{name}: {image} floats"


def test_v2s_images_are_pinned_by_release_version():
    """As v1 pins (fishsense-lite deploy/incus/compose.yml): by `vX.Y.Z`, which
    promote.yml retags from the release commit's SHA image."""
    ours = {
        name: s["image"]
        for name, s in services().items()
        if s["image"].startswith(GHCR)
    }
    assert ours, "no v2 image in the production compose"
    for name, image in ours.items():
        assert image.split("/")[-1].split(":")[0] in IMAGES, f"{name}: {image}"
        assert VERSION.match(image_version(image) or ""), f"{name}: {image}"


def test_one_release_versions_every_v2_image_and_the_processor():
    """One release version for the monorepo (release-please-config.json): an
    API from one release beside an orchestrator from another is a combination
    nobody tested. The processor's tag, which the orchestrator applies on NRP,
    is the same version -- it is set here, not baked into the orchestrator,
    so a release moves it with the rest."""
    versions = {
        image_version(s["image"])
        for s in services().values()
        if image_version(s["image"])
    }
    versions.add(environment(services()["orchestrator"])["FISHSENSE_NRP_IMAGE_TAG"])

    assert len(versions) == 1, versions


def test_the_images_the_compose_runs_are_the_ones_ci_builds():
    ran = {
        s["image"].split("/")[-1].split(":")[0]
        for s in services().values()
        if s["image"].startswith(GHCR)
    }
    assert ran == {
        "fishsense-services-api",
        "fishsense-services-orchestrator",
        "fishsense-services-backup",
        "fishsense-services-web",
    }


# --- state carried over from v1 ---------------------------------------------------


def test_postgres_is_v1s_container_on_v1s_volume():
    """Decision 3: the same instance. The compose project is `fishsense` in
    both (the composeStack's project directory, /var/lib/krg/fishsense), so a
    volume key names the same docker volume v1 wrote: `pgdata` IS v1's
    `fishsense_pgdata`. Renamed, the converge would start an EMPTY cluster and
    the migration would find no v1 database."""
    postgres = services()["postgres"]

    assert postgres["image"] == "postgres:17.10"
    assert ("pgdata", "/var/lib/postgresql/data") in volumes(postgres)
    assert set(compose()["volumes"]) >= {"pgdata", "superset_home", "valkey_data"}


def test_the_interior_network_keeps_v1s_name():
    """pg_hba.conf admits the roles by the bridge's address range; the same
    network keeps the same range."""
    assert list(compose()["networks"]) == ["interior"]


def test_no_initdb_scripts_on_an_existing_cluster():
    """initdb runs only on an empty data directory, which production never has
    (v1's pg_volumes/scripts/README.md). v2's roles come from the bootstrap
    one-shot instead, on every converge."""
    targets = [t for _, t in volumes(services()["postgres"])]
    assert "/docker-entrypoint-initdb.d" not in targets


# --- ordering ----------------------------------------------------------------------


def _waits_for(service: dict, other: str, condition: str) -> bool:
    depends = service.get("depends_on") or {}
    return (
        isinstance(depends, dict)
        and depends.get(other, {}).get("condition") == condition
    )


def test_migrate_runs_after_the_bootstrap():
    """The owner role and the database are the bootstrap's; migrating before
    them fails on the first converge after the switch."""
    assert _waits_for(
        services()["migrate"], "db-bootstrap", "service_completed_successfully"
    )
    assert _waits_for(services()["db-bootstrap"], "postgres", "service_healthy")


@pytest.mark.parametrize("name", ["api", "orchestrator", "backup"])
def test_the_database_users_start_only_on_a_migrated_schema(name):
    """A failed migration must leave the old containers down, not new code on
    an old schema. `service_completed_successfully` fails the converge, and
    krg's stack unit goes red."""
    assert _waits_for(services()[name], "migrate", "service_completed_successfully")


@pytest.mark.parametrize("name", ["db-bootstrap", "migrate", "nrp-temporal-cert-sync"])
def test_one_shots_are_not_restarted(name):
    assert services()[name].get("restart") == "no"


def test_the_bootstrap_runs_as_the_admin_role():
    """Creating a BYPASSRLS role takes a superuser (PLAN.md §6.4: migrate-v1
    needs one): the container's `postgres`, never an app credential."""
    env = service_env(services()["db-bootstrap"])
    assert env["PGUSER"] == "postgres"
    assert env["PGPASSWORD"]


def test_the_bootstrap_script_is_mounted_from_the_repo():
    bootstrap = services()["db-bootstrap"]
    source = dict(volumes(bootstrap))
    assert "./db_bootstrap" in source
    assert (INCUS / "db_bootstrap" / "bootstrap.sh").is_file()


# --- Temporal and NRP ---------------------------------------------------------------


def test_the_reload_list_is_exactly_the_services_that_mount_the_temporal_cert():
    """The 2026-08-17 outage (fishsense-lite flake.nix): a worker keeps the
    leaf it connected with, so a rotation reaches only the services
    `temporal.reload` restarts. A service that mounts the cert and is missing
    from the list holds an expired leaf ~7 days later; one listed that doesn't
    exist makes the hook's `docker compose restart` fail."""
    mounting = {
        n
        for n, s in converged_services().items()
        if mounts(s, f"{TENANT_RUN}/temporal")
    }
    assert set(reload_list()) == mounting
    assert mounting == {"orchestrator", "backup", "nrp-temporal-cert-sync"}


def test_nothing_in_the_reload_list_hides_behind_a_profile():
    """The hook runs `docker compose restart <svc>` with no --profile: a
    profiled service is unknown to it."""
    for name in reload_list():
        assert name in services(), name
        assert not services()[name].get("profiles"), name


def test_every_env_path_under_run_tenant_is_mounted():
    """A settings path that isn't mounted fails at startup (the orchestrator
    reads its cert when it connects), or worse, reads as "absent"."""
    for name, service in services().items():
        for key, value in service_env(service).items():
            if value.startswith(TENANT_RUN + "/"):
                directory = value.rsplit("/", 1)[0]
                assert mounts(service, directory), f"{name}: {key}={value}"


def test_the_orchestrator_stands_the_processor_up_on_nrp():
    """Decision 5. Without a kubeconfig path the wakes are no-ops and no stage
    that needs the processor runs -- silently, as v1's GC'd Deployments did
    for five days."""
    env = environment(services()["orchestrator"])
    assert env["FISHSENSE_NRP_KUBECONFIG_PATH"] == f"{TENANT_RUN}/nrp/kubeconfig"
    assert env["FISHSENSE_NRP_NAMESPACE"] == "e4e-fishsense"
    # The manifests are the image's own (Dockerfile: /app/deploy/nrp).
    assert "FISHSENSE_NRP_MANIFEST_DIR" not in env


def test_the_cert_sync_forwards_the_orchestrators_own_leaf_to_its_cluster():
    orchestrator = environment(services()["orchestrator"])
    sync = services()["nrp-temporal-cert-sync"]
    env = environment(sync)
    for key in (
        "FISHSENSE_TEMPORAL_CLIENT_CERT",
        "FISHSENSE_TEMPORAL_CLIENT_PRIVATE_KEY",
        "FISHSENSE_TEMPORAL_SERVER_ROOT_CA_CERT",
        "FISHSENSE_NRP_KUBECONFIG_PATH",
        "FISHSENSE_NRP_NAMESPACE",
    ):
        assert env[key] == orchestrator[key], key
    assert sync["image"] == services()["orchestrator"]["image"]
    assert sync["command"][-1] == "fishsense_services_orchestrator.ops.cert_sync"


def test_the_cert_sync_timer_restarts_the_compose_container():
    """cert-sync-timer.nix re-runs the one-shot by container name; compose
    must give it that name, or the timer starts nothing."""
    name = services()["nrp-temporal-cert-sync"]["container_name"]
    timer = (INCUS / "cert-sync-timer.nix").read_text()
    assert f"start --attach {name}" in timer


# --- secrets --------------------------------------------------------------------------


def test_every_env_file_is_a_render_and_every_render_is_used():
    """vault-agent is fail-closed on a referenced path but compose is
    fail-closed on a missing env_file: both have to agree, file by file."""
    used = set()
    for name, service in services().items():
        for path in env_files(service):
            if path.startswith(TENANT_RUN):
                assert path in renders(), f"{name}: {path} is rendered by nothing"
                used.add(path)
    rendered_env_files = {d for d in renders() if d.endswith(".env")}
    assert rendered_env_files == used


def test_only_the_nrp_kubeconfig_render_is_soft():
    """Soft (errorOnMissingKey=false) means "render empty": right for the NRP
    kubeconfig, whose absence must not take down the fishsense.vm cert and
    with it the whole slot (v1 secrets.nix). Wrong for anything the stack
    needs to start, which must fail closed."""
    soft = {d for d, r in renders().items() if r.soft}
    assert soft == {f"{TENANT_RUN}/nrp/kubeconfig"}


def test_no_secret_is_committed_in_the_compose():
    """Anything that looks like a credential comes from a render. Paths to a
    render (a cert, a key file) are fine."""
    secretish = re.compile(
        r"PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|DATABASE_URL|AUTH_SECRET"
    )
    for name, service in services().items():
        for key, value in environment(service).items():
            if secretish.search(key) and not value.startswith(TENANT_RUN):
                pytest.fail(f"{name}: {key} is set in the compose, not rendered")


def test_every_rendered_value_comes_from_openbao():
    """A render line whose value is literal is a committed secret."""
    for render in renders().values():
        for name, template in render.variables.items():
            assert "{{" in template, f"{render.destination}: {name} is literal"
            assert render.sources[name], f"{render.destination}: {name} reads no field"


def test_every_database_url_render_escapes_the_password():
    """A password is interpolated into a URL: `@`, `/` or `:` in it would
    re-parse the DSN. `urlquery` escapes them (and the seeds are hex anyway,
    docs/cutover.md)."""
    for render in renders().values():
        for name, template in render.variables.items():
            if name.endswith("DATABASE_URL"):
                assert re.search(r"\{\{[^}]*\|\s*urlquery\s*\}\}", template), name


# --- who holds which credential -----------------------------------------------------


def _holders(variable: str) -> set[str]:
    return {n for n, s in services().items() if variable in service_env(s)}


def test_only_the_migrations_hold_the_owners_dsn():
    """PLAN.md §9.10: the API runs as the app role and is never configured
    with the owner's DSN (settings.MigrationSettings). The smoke test reads the
    schema as the owner, as `migrate` does, and is run by hand."""
    assert _holders("FISHSENSE_MIGRATION_DATABASE_URL") == {"migrate", "smoke"}


def test_only_the_backup_holds_the_backup_credential():
    """The backup role bypasses RLS and reads every tenant; the orchestrator
    must never hold it (ops/backup/settings.py). The bootstrap sets it."""
    assert _holders("FISHSENSE_BACKUP_DATABASE_PASSWORD") == {"backup"}


def test_the_web_holds_no_database_credential():
    """It is the internet-facing process; it reaches data only through the API."""
    env = service_env(services()["web"])
    assert not [k for k in env if "DATABASE" in k or k.startswith("FISHSENSE_NAS")]


def test_the_runtime_roles_and_databases():
    api = service_env(services()["api"])["FISHSENSE_DATABASE_URL"]
    orchestrator = service_env(services()["orchestrator"])["FISHSENSE_DATABASE_URL"]
    migrate = service_env(services()["migrate"])

    assert api == orchestrator
    assert re.fullmatch(rf"postgresql\+asyncpg://fishsense_app:[^@]+@postgres:5432/{V2_DATABASE}", api)  # fmt: skip
    assert re.fullmatch(
        rf"postgresql\+psycopg://fishsense_owner:[^@]+@postgres:5432/{V2_DATABASE}",
        migrate["FISHSENSE_MIGRATION_DATABASE_URL"],
    )
    # migrate-v1 reads v1's database read-only: as the backup role, which holds
    # pg_read_all_data and writes nothing -- v1's database is never modified.
    assert re.fullmatch(
        rf"postgresql\+psycopg://fishsense_backup:[^@]+@postgres:5432/{V1_DATABASE}",
        migrate["FISHSENSE_V1_DATABASE_URL"],
    )


def test_the_api_accepts_the_webs_tokens():
    """The web's access tokens carry its client id as `aud`; the API must list
    it (decision 7: OIDC audiences for web), and trust the issuer that signs
    them."""
    api = renders()[f"{TENANT_RUN}/secrets/api.env"].sources
    web = renders()[f"{TENANT_RUN}/secrets/web.env"].sources
    assert api["FISHSENSE_OIDC_AUDIENCES"] == web["AUTH_AUTHENTIK_ID"]
    assert api["FISHSENSE_OIDC_ISSUER"] == web["AUTH_AUTHENTIK_ISSUER"]


# --- the backup ------------------------------------------------------------------------


def test_the_backup_dumps_v2_v1s_archive_and_superset():
    """Decision 8: v2's backup replaces v1's backup-worker, and v1's database
    is the rollback's and the archive's."""
    env = environment(services()["backup"])
    import json

    assert set(json.loads(env["FISHSENSE_BACKUP_DATABASES"])) == {
        V2_DATABASE,
        V1_DATABASE,
        "superset",
    }


def test_the_backup_writes_where_v1s_pruning_cannot_reach():
    """v1's dumps live at {root}/fishsense/*.dump under v1's root, and both
    backups keep 14 per folder. Sharing v1's root, v2's dump of the `fishsense`
    database would land in v1's folder and v2's prune would delete v1's last
    pre-cutover dumps: the rollback's insurance."""
    env = environment(services()["backup"])
    assert (
        env["FISHSENSE_BACKUP_NAS_ROOT_PATH"]
        != "/fishsense_process_work/database_backups"
    )
    assert env["FISHSENSE_BACKUP_DATABASE_USER"] == "fishsense_backup"


# --- the working directory -------------------------------------------------------------


def test_every_relative_bind_is_linked_into_the_working_directory():
    """compose resolves `./x` against /var/lib/krg/fishsense, not the store;
    workdir.nix links each there. A forgotten one is silent: docker creates the
    missing source as an EMPTY directory (v1 workdir.nix, nrp_cert_sync)."""
    linked = set(re.findall(r"/var/lib/krg/fishsense/(\S+)", WORKDIR_NIX.read_text()))
    for name, service in services().items():
        sources = [s for s, _ in volumes(service)] + env_files(service)
        for source in sources:
            if source.startswith("./"):
                top = source[2:].split("/")[0]
                assert top in linked, f"{name}: {source} is not linked"
                assert (INCUS / top).exists(), f"{name}: {source} is not committed"


def test_compose_env_is_linked_as_the_project_env():
    """`.env` in the project directory is how compose learns COMPOSE_PROFILES."""
    assert re.search(
        r"/var/lib/krg/fishsense/\.env\s.*compose\.env", WORKDIR_NIX.read_text()
    )


# --- routing -----------------------------------------------------------------------------


def _dynamic() -> dict:
    import yaml

    return yaml.safe_load((INCUS / "traefik-dynamic.yml").read_text())


def test_the_three_hosts_route_to_the_three_services():
    http = _dynamic()["http"]
    hosts = {
        re.search(r"Host\(`([^`]+)`\)", r["rule"]).group(1): r["service"]
        for r in http["routers"].values()
    }
    assert hosts == {
        "fishsense.e4e.ucsd.edu": "web",
        "api.fishsense.e4e.ucsd.edu": "api",
        "analytics.fishsense.e4e.ucsd.edu": "superset",
    }
    for name, backend in http["services"].items():
        url = backend["loadBalancer"]["servers"][0]["url"]
        host, port = re.fullmatch(r"http://([a-z-_]+):(\d+)", url).groups()
        assert host in services(), f"{name} -> {host}: no such service"
        assert port in [str(p) for p in services()[host].get("expose", [])], url


def test_the_api_route_has_no_forward_auth():
    """v2's API validates bearer tokens itself (auth.TokenValidator). v1's
    forwardAuth outpost in front of it would 302 every API client to a login
    page; it and its outpost are gone."""
    http = _dynamic()["http"]
    assert "middlewares" not in http
    assert all("middlewares" not in r for r in http["routers"].values())
    assert "authentik-outpost" not in services()


def test_every_route_serves_the_vault_agent_cert():
    tls = _dynamic()["tls"]
    cert = tls["stores"]["default"]["defaultCertificate"]["certFile"]
    assert cert == "/etc/traefik/tls/fishsense.vm.crt"
    assert (f"{TENANT_RUN}/tls", "/etc/traefik/tls") in volumes(services()["traefik"])


# --- superset ------------------------------------------------------------------------------


SUPERSET = (
    "superset",
    "superset-init",
    "superset-worker",
    "superset-worker-beat",
    "valkey",
)


def test_superset_is_off_unless_its_profile_is_named():
    """Kept from v1, behind the `superset` profile: off until the analytics
    login is bound to the lab (docs/cutover.md), then on by one line in
    compose.env."""
    for name in SUPERSET:
        assert services()[name].get("profiles") == ["superset"], name
    assert re.search(
        r"^COMPOSE_PROFILES=\s*$", (INCUS / "compose.env").read_text(), re.M
    )


def test_the_smoke_test_runs_only_when_asked():
    assert services()["smoke"].get("profiles") == ["ops"]
    assert services()["smoke"]["image"] == services()["orchestrator"]["image"]
