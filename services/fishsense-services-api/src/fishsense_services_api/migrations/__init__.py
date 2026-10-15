"""Schema migrations, run programmatically (no alembic.ini).

Migrations run as the schema owner, never as the app role: the app role must
not own the tables it queries, or FORCE ROW LEVEL SECURITY is its only guard.
"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

_SCRIPT_LOCATION = Path(__file__).parent


def _config(database_url: str, app_role: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["app_role"] = app_role
    return config


def head_revision() -> str:
    """The newest migration's revision id."""
    return ScriptDirectory.from_config(_config("", "")).get_current_head()


def is_known_revision(revision: str) -> bool:
    """Whether ``revision`` is one of this release's migrations."""
    try:
        return (
            ScriptDirectory.from_config(_config("", "")).get_revision(revision)
            is not None
        )
    except CommandError:
        return False


async def current_revision(database_url: str) -> str | None:
    """The revision the database is at; None if it was never migrated."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            if not await conn.run_sync(
                lambda sync: inspect(sync).has_table("alembic_version")
            ):
                return None
            return (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    finally:
        await engine.dispose()


async def upgrade(database_url: str, *, app_role: str, revision: str = "head") -> None:
    """Migrate to ``revision``, granting the runtime privileges to ``app_role``."""
    await asyncio.to_thread(command.upgrade, _config(database_url, app_role), revision)
