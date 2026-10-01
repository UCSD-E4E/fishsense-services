"""Shared fixtures: one real Postgres per test session, migrated once.

Tenancy is a property of the database (roles, grants, RLS policies), so these
tests run against real Postgres -- never SQLite, never mocks. Two engines:

- ``owner_engine`` connects as the container's superuser, which owns the
  schema and bypasses RLS. Tests use it only to seed and inspect.
- ``app_engine`` connects as ``fishsense_app``, the unprivileged role the API
  runs as. Every assertion about isolation goes through this one.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api.migrations import upgrade

TIERS = {"unit", "integration", "e2e"}


@pytest.hookimpl(tryfirst=True)  # before `-m` deselects anything
def pytest_collection_modifyitems(config, items):
    """Assign each test its tier from what it actually touches.

    A test whose fixtures reach the Postgres container is ``integration``; one
    marked ``integration``/``e2e`` explicitly keeps that; everything else is
    ``unit``. A test marked ``unit`` that reaches Postgres is an error, so the
    unit tier can't quietly start needing Docker.
    """
    for item in items:
        declared = {mark.name for mark in item.iter_markers()} & TIERS
        if "postgres" in item.fixturenames:
            if "unit" in declared:
                raise pytest.UsageError(
                    f"{item.nodeid} is marked unit but uses Postgres"
                )
            item.add_marker(pytest.mark.integration)
        elif not declared:
            item.add_marker(pytest.mark.unit)


POSTGRES_IMAGE = "postgres:17.10"
APP_ROLE = "fishsense_app"
APP_PASSWORD = "fishsense_app"


@pytest.fixture(scope="session")
def postgres() -> PostgresContainer:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def owner_url(postgres: PostgresContainer) -> str:
    return postgres.get_connection_url()


@pytest.fixture(scope="session")
def app_url(postgres: PostgresContainer) -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(postgres.port)
    return f"postgresql+asyncpg://{APP_ROLE}:{APP_PASSWORD}@{host}:{port}/{postgres.dbname}"


@pytest.fixture(scope="session")
async def owner_engine(owner_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(owner_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
        )
    await upgrade(owner_url, app_role=APP_ROLE)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def app_engine(
    owner_engine: AsyncEngine, app_url: str
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def seed_memberships(owner_engine: AsyncEngine):
    """Seed as the owner: ``{sub: {tenant_slug: role}}`` -> tenant ids by slug.

    Memberships are granted administratively, never through the API, so tests
    create them the way an admin would: directly, as the schema owner.
    """

    async def seed(memberships: dict[str, dict[str, str]]) -> dict[str, uuid.UUID]:
        slugs = {slug for tenants in memberships.values() for slug in tenants}
        async with owner_engine.begin() as conn:
            tenant_ids = {}
            for slug in sorted(slugs):
                tenant_ids[slug] = (
                    await conn.execute(
                        text(
                            "INSERT INTO tenants (slug, name) VALUES (:s, :s) "
                            "RETURNING id"
                        ),
                        {"s": slug},
                    )
                ).scalar_one()
            for sub, tenants in memberships.items():
                user_id = (
                    await conn.execute(
                        text("INSERT INTO users (sub) VALUES (:sub) RETURNING id"),
                        {"sub": sub},
                    )
                ).scalar_one()
                for slug, role in tenants.items():
                    await conn.execute(
                        text(
                            "INSERT INTO memberships (tenant_id, user_id, role) "
                            "VALUES (:t, :u, :r)"
                        ),
                        {"t": tenant_ids[slug], "u": user_id, "r": role},
                    )
        return tenant_ids

    return seed


@pytest.fixture(autouse=True)
async def clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "owner_engine" not in request.fixturenames:
        return
    owner_engine: AsyncEngine = request.getfixturevalue("owner_engine")
    async with owner_engine.begin() as conn:
        await conn.execute(text("TRUNCATE tenants, users CASCADE"))
