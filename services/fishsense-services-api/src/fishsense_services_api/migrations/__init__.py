"""Schema migrations, run programmatically (no alembic.ini).

Migrations run as the schema owner, never as the app role: the app role must
not own the tables it queries, or FORCE ROW LEVEL SECURITY is its only guard.
"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

_SCRIPT_LOCATION = Path(__file__).parent


def _config(database_url: str, app_role: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["app_role"] = app_role
    return config


async def upgrade(database_url: str, *, app_role: str) -> None:
    """Migrate to head, granting the runtime privileges to ``app_role``."""
    await asyncio.to_thread(command.upgrade, _config(database_url, app_role), "head")
