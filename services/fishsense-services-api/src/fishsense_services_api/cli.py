"""``fishsense-services-api`` command line.

fishsense-services-api migrate   # bring the schema to head, as the owner
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.migrations import head_revision, upgrade
from fishsense_services_api.schema_audit import tenancy_violations
from fishsense_services_api.settings import MigrationSettings

ENV_PREFIX = "FISHSENSE_"


async def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="fishsense-services-api")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="migrate the schema to head, as its owner")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        return await _migrate()
    return 2  # unreachable: argparse rejects unknown commands


async def _migrate() -> int:
    try:
        settings = MigrationSettings()
    except ValidationError as error:
        names = sorted(f"{ENV_PREFIX}{e['loc'][0]}".upper() for e in error.errors())
        print(f"missing or invalid configuration: {', '.join(names)}", file=sys.stderr)
        return 2
    database_url = settings.migration_database_url.get_secret_value()
    try:
        await upgrade(database_url, app_role=settings.app_role)
    except Exception as error:  # report, don't traceback: this is a deploy step
        print(f"migration FAILED -- nothing applied: {error}", file=sys.stderr)
        return 1
    print(f"schema at revision {head_revision()}")

    violations = await _audit(database_url, settings.app_role)
    if violations:
        print("tenancy audit FAILED -- do not deploy:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    print("tenancy audit passed")
    return 0


async def _audit(database_url: str, app_role: str) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            return await tenancy_violations(conn, app_role=app_role)
    finally:
        await engine.dispose()


def run() -> None:
    """Console-script entry point."""
    sys.exit(asyncio.run(main(sys.argv[1:])))
