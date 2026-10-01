"""The bootstrap and `migrate` against real Postgres, shaped like the slot's.

The slot's cluster is v1's: a `fishsense` database, v1's roles, and v1's
postgres.conf and pg_hba.conf (kept in deploy/incus/pg_volumes). v2 arrives on
it with no initdb (the data directory isn't empty), so deploy/incus/
db_bootstrap/bootstrap.sh creates v2's database and roles as the admin, and
then `migrate` runs as the owner it made -- **not a superuser**, which nothing
else in the repo exercises: every other suite migrates as the container's
superuser. This is where "the owner can migrate, and migrate-v1 will accept
it" is proven before the cutover rather than during it.

The server runs with the committed config, so the new roles' pg_hba lines are
tested too: the test's connections arrive from the Docker bridge, as the
interior network's do on the slot.
"""

from __future__ import annotations

import secrets
import asyncio
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from testcontainers.community.postgres import PostgresContainer

from _deploy import INCUS, REPO

pytestmark = pytest.mark.integration

#: Made up per run, so no credential-shaped literal sits in the source for a
#: secret scanner to flag (GitGuardian did, on fixed fake ones).
ADMIN_PASSWORD = secrets.token_hex(8)
PASSWORDS = {
    "FISHSENSE_OWNER_PASSWORD": secrets.token_hex(8),
    "FISHSENSE_APP_PASSWORD": secrets.token_hex(8),
    "FISHSENSE_BACKUP_PASSWORD": secrets.token_hex(8),
    "FISHSENSE_ANALYTICS_PASSWORD": secrets.token_hex(8),
    "FISHSENSE_SMOKE_PASSWORD": secrets.token_hex(8),
}
#: v1's own login roles on the slot-shaped cluster.
V1_SUPERSET_PASSWORD = secrets.token_hex(8)
V1_BACKUP_PASSWORD = secrets.token_hex(8)
V2 = "fishsense_services"


@pytest.fixture(scope="module")
def cluster():
    container = (
        PostgresContainer(
            "postgres:17.10",
            username="postgres",
            password=ADMIN_PASSWORD,
            dbname="postgres",
            driver="psycopg",
        )
        .with_volume_mapping(
            str(INCUS / "pg_volumes" / "config"), "/etc/postgresql", "ro"
        )
        .with_volume_mapping(str(INCUS / "db_bootstrap"), "/bootstrap", "ro")
        .with_command("--config_file=/etc/postgresql/postgres.conf")
    )
    with container:
        _v1_as_the_slot_has_it(container)
        yield container


def _url(container, user: str, password: str, database: str) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


def _admin(container, database: str = "postgres"):
    return create_engine(
        _url(container, "postgres", ADMIN_PASSWORD, database),
        isolation_level="AUTOCOMMIT",
    )


def _v1_as_the_slot_has_it(container) -> None:
    """v1's database with a row, and v1's roles, as the restore left them."""
    admin = _admin(container)
    with admin.connect() as conn:
        conn.execute(
            text(f"CREATE ROLE superset LOGIN PASSWORD '{V1_SUPERSET_PASSWORD}'")
        )
        conn.execute(text(f"CREATE ROLE backup LOGIN PASSWORD '{V1_BACKUP_PASSWORD}'"))
        conn.execute(text("CREATE DATABASE fishsense"))
        conn.execute(text("CREATE DATABASE superset OWNER superset"))
    admin.dispose()
    v1 = _admin(container, "fishsense")
    with v1.connect() as conn:
        conn.execute(text("CREATE TABLE dive (id int PRIMARY KEY, name text)"))
        conn.execute(text("INSERT INTO dive VALUES (490, 'Nathans Pool')"))
    v1.dispose()


def _bootstrap(container, **overrides) -> tuple[int, str]:
    env = {
        "PGHOST": "127.0.0.1",
        "PGUSER": "postgres",
        "PGPASSWORD": ADMIN_PASSWORD,
        **PASSWORDS,
        **overrides,
    }
    assignments = " ".join(f"{k}='{v}'" for k, v in env.items())
    code, output = container.exec(
        ["sh", "-c", f"{assignments} sh /bootstrap/bootstrap.sh"]
    )
    return code, output.decode()


@pytest.fixture(scope="module")
def migrated(cluster):
    """The first converge: bootstrap, then migrate as the owner."""
    code, output = _bootstrap(cluster)
    assert code == 0, output

    from fishsense_services_api.migrations import upgrade

    owner = _url(cluster, "fishsense_owner", PASSWORDS["FISHSENSE_OWNER_PASSWORD"], V2)
    asyncio.run(upgrade(owner, app_role="fishsense_app"))
    return cluster


def _scalar(url: str, sql: str, **params):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _login(cluster, role: str, database: str = V2) -> str:
    key = {
        "fishsense_owner": "FISHSENSE_OWNER_PASSWORD",
        "fishsense_app": "FISHSENSE_APP_PASSWORD",
        "fishsense_backup": "FISHSENSE_BACKUP_PASSWORD",
        "fishsense_superset": "FISHSENSE_ANALYTICS_PASSWORD",
        "fishsense_smoke": "FISHSENSE_SMOKE_PASSWORD",
    }[role]
    return _url(cluster, role, PASSWORDS[key], database)


# --- the first converge ------------------------------------------------------------


def test_the_owner_migrates_to_head_without_being_a_superuser(migrated):
    from fishsense_services_api.migrations import head_revision

    owner = _login(migrated, "fishsense_owner")
    assert _scalar(owner, "SELECT version_num FROM alembic_version") == head_revision()
    assert _scalar(owner, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user") is False  # fmt: skip


async def test_the_tenancy_audit_passes_as_migrate_runs_it(migrated):
    from sqlalchemy.ext.asyncio import create_async_engine

    from fishsense_services_api.schema_audit import tenancy_violations

    engine = create_async_engine(_login(migrated, "fishsense_owner"))
    try:
        async with engine.connect() as conn:
            assert await tenancy_violations(conn, app_role="fishsense_app") == []
    finally:
        await engine.dispose()


def test_migrate_v1_will_accept_the_owner(migrated):
    """Its preflight refuses a target role that can't bypass RLS."""
    from fishsense_services_api.migrations import head_revision
    from fishsense_services_api.v1_migration import preflight

    assert preflight(_login(migrated, "fishsense_owner"), head_revision()) == []


def test_the_owner_has_no_statement_timeout(migrated):
    """v1's postgres.conf sets 30 s for everyone; a migration isn't a query."""
    owner = _login(migrated, "fishsense_owner")
    app = _login(migrated, "fishsense_app")
    assert _scalar(owner, "SHOW statement_timeout") == "0"
    assert _scalar(app, "SHOW statement_timeout") == "30s"


def test_the_app_role_connects_under_rls(migrated):
    app = _login(migrated, "fishsense_app")
    assert _scalar(app, "SELECT count(*) FROM dives") == 0
    assert _scalar(app, "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user") is False  # fmt: skip


def test_v1s_roles_cannot_connect_to_v2s_database(migrated):
    with pytest.raises(OperationalError, match="permission denied for database"):
        _scalar(_url(migrated, "superset", V1_SUPERSET_PASSWORD, V2), "SELECT 1")


# --- v1's database: read by the backup, never written ------------------------------------


def test_the_backup_reads_v1s_archive_and_superset_and_writes_neither(migrated):
    archive = _login(migrated, "fishsense_backup", "fishsense")
    assert _scalar(archive, "SELECT name FROM dive WHERE id = 490") == "Nathans Pool"
    assert _scalar(_login(migrated, "fishsense_backup", "superset"), "SELECT 1") == 1
    with pytest.raises(ProgrammingError, match="permission denied"):
        _scalar(archive, "INSERT INTO dive VALUES (1, 'x') RETURNING id")


def test_v1s_database_is_untouched(migrated):
    v1 = _admin(migrated, "fishsense")
    with v1.connect() as conn:
        owner = conn.execute(
            text(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'fishsense'"
            )
        ).scalar()
        tables = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        ).scalar()
    v1.dispose()
    assert (owner, tables) == ("postgres", 1)


# --- every later converge -----------------------------------------------------------------


def test_a_second_run_changes_nothing_and_succeeds(migrated):
    code, output = _bootstrap(migrated)
    assert code == 0, output
    from fishsense_services_api.migrations import head_revision

    owner = _login(migrated, "fishsense_owner")
    assert _scalar(owner, "SELECT version_num FROM alembic_version") == head_revision()


def test_a_rotated_password_reaches_postgres_on_the_next_converge(migrated):
    rotated = secrets.token_hex(8)
    code, output = _bootstrap(migrated, FISHSENSE_APP_PASSWORD=rotated)
    assert code == 0, output
    try:
        url = _url(migrated, "fishsense_app", rotated, V2)
        assert _scalar(url, "SELECT 1") == 1
        with pytest.raises(OperationalError, match="password authentication failed"):
            _scalar(_login(migrated, "fishsense_app"), "SELECT 1")
    finally:
        assert _bootstrap(migrated)[0] == 0


def test_a_missing_password_refuses_to_run(migrated):
    code, output = _bootstrap(migrated, FISHSENSE_SMOKE_PASSWORD="")
    assert code != 0
    assert "FISHSENSE_SMOKE_PASSWORD" in output


# --- after migrate-v1: the analytics and research logins ---------------------------------


@pytest.fixture(scope="module")
def with_lab(migrated):
    """What migrate-v1 leaves: the lab tenant (and a dive in it), then the
    converge after it, which binds Superset's login."""
    lab = uuid.uuid4()
    owner = create_engine(_login(migrated, "fishsense_owner"))
    with owner.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, 'lab', 'Lab')"),
            {"id": lab},
        )
    owner.dispose()
    code, output = _bootstrap(migrated)
    assert code == 0, output
    assert "binding fishsense_superset to the lab" in output
    return migrated, lab


def test_superset_is_bound_to_the_lab_in_v2s_database_only(with_lab):
    cluster, lab = with_lab
    superset = _login(cluster, "fishsense_superset")
    assert _scalar(superset, "SELECT current_setting('app.tenant_id', true)") == str(
        lab
    )
    admin = _admin(cluster)
    with admin.connect() as conn:
        settings = conn.execute(
            text(
                "SELECT d.datname, s.setconfig FROM pg_db_role_setting s "
                "JOIN pg_roles r ON r.oid = s.setrole "
                "JOIN pg_database d ON d.oid = s.setdatabase "
                "WHERE r.rolname = 'fishsense_superset'"
            )
        ).all()
    admin.dispose()
    assert settings == [(V2, [f"app.tenant_id={lab}"])]


@pytest.mark.parametrize(
    "dataset", ["pipeline_labeling_queue", "pipeline_partial_dives"]
)
def test_the_pipeline_dashboards_datasets_run_as_superset(with_lab, dataset):
    cluster, _ = with_lab
    sql = (REPO / "deploy" / "superset" / "datasets" / f"{dataset}.sql").read_text()
    engine = create_engine(_login(cluster, "fishsense_superset"))
    try:
        with engine.connect() as conn:
            conn.execute(text(sql)).all()
    finally:
        engine.dispose()


def test_the_physical_dataset_has_every_column_the_bundle_names(with_lab):
    import yaml

    cluster, _ = with_lab
    bundle = INCUS / "superset_volumes/docker/assets/datasets/FishSense"
    wanted = {c["column_name"] for c in yaml.safe_load((bundle / "dive_pipeline_status.yaml").read_text())["columns"]}  # fmt: skip
    engine = create_engine(_login(cluster, "fishsense_superset"))
    try:
        with engine.connect() as conn:
            have = set(conn.execute(text("SELECT * FROM dive_pipeline_status LIMIT 0")).keys())  # fmt: skip
    finally:
        engine.dispose()
    assert wanted <= have, wanted - have


def test_fish_measurements_needs_the_research_grant(with_lab):
    """What breaks from v1's bundle: fishsense_analytics reads the pipeline
    views only (0030), not the `v1` research views the fish-measurements
    dataset needs. docs/cutover.md: granting `fishsense_research` to the
    Superset login is the owner's call (PLAN.md §9.18); this pins both sides."""
    import yaml

    cluster, _ = with_lab
    bundle = INCUS / "superset_volumes/docker/assets/datasets/FishSense"
    sql = yaml.safe_load((bundle / "fish_measurements.yaml").read_text())["sql"]
    url = _login(cluster, "fishsense_superset")
    with pytest.raises(ProgrammingError, match="permission denied"):
        _scalar(url, sql)
    admin = _admin(cluster)
    with admin.connect() as conn:
        conn.execute(text("GRANT fishsense_research TO fishsense_superset"))
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text(sql)).all()
        engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text("REVOKE fishsense_research FROM fishsense_superset"))
        admin.dispose()


def test_the_smoke_login_reads_the_research_views(with_lab):
    cluster, _ = with_lab
    count = _scalar(
        _login(cluster, "fishsense_smoke"),
        "SELECT count(*) FROM v1.measurement m JOIN v1.image i ON i.id = m.image_id "
        "WHERE i.dive_id = :dive AND m.length_m IS NOT NULL",
        dive=490,
    )
    assert count == 0


def test_the_runbooks_membership_grant_is_idempotent(with_lab):
    """docs/cutover.md step 4d's SQL, verbatim with the subs filled in: the
    orchestrator and the web's service account as members, an admin as admin.
    Run twice (a re-run on the night must not fail or duplicate)."""
    import re

    cluster, lab = with_lab
    runbook = (REPO / "docs" / "cutover.md").read_text()
    sql = re.search(
        r"psql -U postgres -d fishsense_services -v ON_ERROR_STOP=1 <<'SQL'\n(.*?)\nSQL\n",
        runbook,
        re.S,
    ).group(1)
    sql = sql.replace("<web service account sub>", "sub-web-sa").replace(
        "<lab admin sub>", "sub-admin"
    )
    admin = _admin(cluster, V2)
    try:
        for _ in range(2):
            with admin.connect() as conn:
                conn.execute(text(sql))
        with admin.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT u.sub, m.role, m.tenant_id FROM memberships m "
                    "JOIN users u ON u.id = m.user_id ORDER BY u.sub"
                )
            ).all()
    finally:
        admin.dispose()
    assert rows == [
        ("service:fishsense-orchestrator", "member", lab),
        ("sub-admin", "admin", lab),
        ("sub-web-sa", "member", lab),
    ]
