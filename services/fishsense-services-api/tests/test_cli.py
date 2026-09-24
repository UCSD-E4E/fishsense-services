"""``fishsense-services-api migrate``: schema migrations as a separate step.

The running API holds only the unprivileged app role, so it can't migrate its
own schema (PLAN.md §3, §9.10). Migrations run as a one-shot command with the
owner's DSN, which the API process never sees.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.cli import main
from fishsense_services_api.migrations import head_revision

APP_ROLE = "fishsense_app"  # created by the session fixtures in conftest


@pytest.fixture
async def empty_database(owner_engine, owner_url, monkeypatch) -> str:
    """A fresh, unmigrated database on the test server; returns its owner DSN."""
    name = f"migrate_{uuid.uuid4().hex[:8]}"
    autocommit = owner_engine.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit.connect() as conn:
        await conn.execute(text(f"CREATE DATABASE {name}"))
    monkeypatch.delenv("FISHSENSE_APP_ROLE", raising=False)
    yield make_url(owner_url).set(database=name).render_as_string(hide_password=False)
    async with autocommit.connect() as conn:
        await conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))


async def _version(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
    finally:
        await engine.dispose()


async def test_migrate_brings_an_empty_database_to_head(
    empty_database, monkeypatch, capsys
):
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", empty_database)

    assert await main(["migrate"]) == 0
    assert await _version(empty_database) == head_revision()
    assert f"schema at revision {head_revision()}" in capsys.readouterr().out


async def test_migrate_twice_is_a_no_op(empty_database, monkeypatch):
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", empty_database)

    assert await main(["migrate"]) == 0
    assert await main(["migrate"]) == 0
    assert await _version(empty_database) == head_revision()


async def test_migrate_refuses_a_schema_that_breaks_tenancy(
    empty_database, monkeypatch, capsys
):
    """A table without isolation fails the deploy step, naming the table."""
    engine = create_async_engine(empty_database)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE rogue (id int PRIMARY KEY)"))
    await engine.dispose()
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", empty_database)

    assert await main(["migrate"]) != 0
    assert "rogue" in capsys.readouterr().err


async def test_migrate_without_an_owner_dsn_fails_naming_it(monkeypatch, capsys):
    monkeypatch.delenv("FISHSENSE_MIGRATION_DATABASE_URL", raising=False)

    assert await main(["migrate"]) != 0
    assert "FISHSENSE_MIGRATION_DATABASE_URL" in capsys.readouterr().err
