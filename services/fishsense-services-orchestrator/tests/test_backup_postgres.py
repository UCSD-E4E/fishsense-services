"""The backup against real Postgres, on v2's schema.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/tests/
test_pg_dump_integration.py (a real dump that `pg_restore -l` can read), plus
the one thing v2 changes underneath it.

**v2 change: the backup role bypasses row-level security.** v1's `backup` role
had `pg_read_all_data` and nothing else (deploy/pg_volumes/scripts/
2026-05-01_create_backup_role.sql), which was enough when no table had RLS.
Every tenant-scoped v2 table has *forced* RLS, and pg_dump runs with
`row_security = off`, so under v1's grant it refuses outright ("query would be
affected by row-level security policy"). Dumping with row security on instead
would silently write out no rows at all: no tenant is set. So the role v2
backs up as has `pg_read_all_data` **and `BYPASSRLS`** -- read everything,
write nothing -- and it is created by deploy/local/initdb/02-backup-role.sh,
which is what these tests run.

pg_dump runs inside the Postgres container (its own binary, matching the
server), so these need Docker but no local Postgres client. `run_pg_dump`
itself is exercised against the container too when a local `pg_dump` exists.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api.migrations import upgrade
from fishsense_services_orchestrator.ops.backup.pg_dump import run_pg_dump

ROLE_SCRIPT = (
    Path(__file__).resolve().parents[3] / "deploy/local/initdb/02-backup-role.sh"
)
APP_ROLE = "fishsense_app"
BACKUP_ROLE = "fishsense_backup"
BACKUP_PASSWORD = "backup-test-only"


@pytest.fixture(scope="module")
def postgres():
    with PostgresContainer("postgres:17.10", driver="asyncpg") as container:
        yield container


async def _migrate(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'app'"))
    await upgrade(url, app_role=APP_ROLE)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (slug, name) VALUES ('lab', 'Lab')")
        )
    await engine.dispose()


@pytest.fixture(scope="module")
def migrated(postgres):
    """v2's schema at head, with one tenant's rows in RLS-forced tables."""
    asyncio.run(_migrate(postgres.get_connection_url()))
    return postgres


@pytest.fixture(scope="module")
def backup_role(migrated):
    """The backup role, created as a fresh deployment creates it."""
    code, out = _exec(
        migrated,
        ["bash", "-c", ROLE_SCRIPT.read_text()],
        POSTGRES_USER=migrated.username,
        POSTGRES_DB=migrated.dbname,
        FISHSENSE_BACKUP_PASSWORD=BACKUP_PASSWORD,
    )
    assert code == 0, out
    return migrated


def _exec(container, cmd, **env):
    exit_code, output = container.get_wrapped_container().exec_run(cmd, environment=env)
    return exit_code, output.decode(errors="replace")


def _dump_in_container(container, role, password, out="/tmp/v2.dump"):
    return _exec(
        container,
        ["pg_dump", "-Fc", "-h", "localhost", "-U", role, "-d", container.dbname,
         "-f", out],
        PGPASSWORD=password,
    )  # fmt: skip


def test_v1s_grant_alone_cannot_dump_v2s_schema(migrated):
    """Why the role changes: `pg_read_all_data` does not reach through forced
    row-level security."""
    code, out = _exec(
        migrated,
        ["psql", "-v", "ON_ERROR_STOP=1", "-U", migrated.username, "-d",
         migrated.dbname, "-c",
         "CREATE ROLE v1_backup LOGIN PASSWORD 'v1' NOBYPASSRLS; "
         "GRANT pg_read_all_data TO v1_backup;"],
    )  # fmt: skip
    assert code == 0, out

    code, out = _dump_in_container(migrated, "v1_backup", "v1", out="/tmp/v1.dump")

    assert code != 0
    assert "row-level security" in out


def test_the_backup_role_dumps_every_tenants_rows(backup_role):
    migrated = backup_role

    code, out = _dump_in_container(migrated, BACKUP_ROLE, BACKUP_PASSWORD)
    assert code == 0, out

    # The archive is a custom-format dump pg_restore can read, and the rows
    # behind RLS are in it -- not an empty table.
    code, toc = _exec(migrated, ["pg_restore", "-l", "/tmp/v2.dump"])
    assert code == 0, toc
    assert "TABLE DATA public tenants" in toc
    code, data = _exec(
        migrated,
        ["pg_restore", "--data-only", "-t", "tenants", "-f", "-", "/tmp/v2.dump"],
    )
    assert code == 0, data
    assert "lab\tLab" in data


def test_the_backup_role_can_read_but_never_write(backup_role):
    """Read everything, write nothing: the role's whole job is pg_dump."""
    migrated = backup_role

    code, out = _exec(
        migrated,
        ["psql", "-v", "ON_ERROR_STOP=1", "-h", "localhost", "-U", BACKUP_ROLE,
         "-d", migrated.dbname, "-c",
         "INSERT INTO tenants (slug, name) VALUES ('x', 'x')"],
        PGPASSWORD=BACKUP_PASSWORD,
    )  # fmt: skip

    assert code != 0
    assert "permission denied" in out


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="no local pg_dump")
def test_run_pg_dump_produces_a_pg_restore_listable_dump(backup_role, tmp_path):
    """v1's integration test: `run_pg_dump` itself, end to end."""
    migrated = backup_role
    out = tmp_path / "v2.dump"

    run_pg_dump(
        db_name=migrated.dbname,
        host=migrated.get_container_host_ip(),
        port=int(migrated.get_exposed_port(migrated.port)),
        username=BACKUP_ROLE,
        password=BACKUP_PASSWORD,
        output_path=str(out),
        timeout_s=300.0,
    )

    assert out.exists() and out.stat().st_size > 1024
    toc = subprocess.run(
        ["pg_restore", "-l", str(out)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "TABLE DATA public tenants" in toc
