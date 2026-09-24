"""Shared fixtures: one real Postgres per test session, migrated once.

Tenancy is a property of the database (roles, grants, RLS policies), so these
tests run against real Postgres -- never SQLite, never mocks. Two engines:

- ``owner_engine`` connects as the container's superuser, which owns the
  schema and bypasses RLS. Tests use it only to seed and inspect.
- ``app_engine`` connects as ``fishsense_app``, the unprivileged role the API
  runs as. Every assertion about isolation goes through this one.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fishsense_services_api.migrations import upgrade

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


@pytest.fixture(autouse=True)
async def clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "owner_engine" not in request.fixturenames:
        return
    owner_engine: AsyncEngine = request.getfixturevalue("owner_engine")
    async with owner_engine.begin() as conn:
        await conn.execute(text("TRUNCATE tenants CASCADE"))
