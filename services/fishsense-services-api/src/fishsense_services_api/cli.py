"""``fishsense-services-api`` command line.

fishsense-services-api migrate   # bring the schema to head, as the owner
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from fishsense_services_api.migrations import upgrade
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
    await upgrade(
        settings.migration_database_url.get_secret_value(),
        app_role=settings.app_role,
    )
    return 0


def run() -> None:
    """Console-script entry point."""
    sys.exit(asyncio.run(main(sys.argv[1:])))
