"""``fishsense-services-api`` command line.

fishsense-services-api migrate      # bring the schema to head, as the owner
fishsense-services-api migrate-v1   # one-shot v1 data migration, with go/no-go
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.migrations import head_revision, upgrade
from fishsense_services_api.schema_audit import tenancy_violations
from fishsense_services_api.settings import MigrationSettings, V1MigrationSettings
from fishsense_services_api.v1_migration import (
    measurement_parity,
    migrate_v1,
    preflight,
)

ENV_PREFIX = "FISHSENSE_"


async def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="fishsense-services-api")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="migrate the schema to head, as its owner")
    commands.add_parser(
        "migrate-v1", help="migrate v1's data into the lab tenant, then validate"
    )
    args = parser.parse_args(argv)

    if args.command == "migrate":
        return await _migrate()
    if args.command == "migrate-v1":
        return await _migrate_v1()
    return 2  # unreachable: argparse rejects unknown commands


async def _migrate() -> int:
    settings = _settings(MigrationSettings)
    if settings is None:
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


def _settings(cls):
    try:
        return cls()
    except ValidationError as error:
        names = sorted(f"{ENV_PREFIX}{e['loc'][0]}".upper() for e in error.errors())
        print(f"missing or invalid configuration: {', '.join(names)}", file=sys.stderr)
        return None


async def _migrate_v1() -> int:
    """Migrate v1 into the lab tenant; exit 0 only on GO (PLAN.md §6.4)."""
    settings = _settings(V1MigrationSettings)
    if settings is None:
        return 2
    source = settings.v1_database_url.get_secret_value()
    target = settings.migration_database_url.get_secret_value()

    problems = await asyncio.to_thread(preflight, target, head_revision())
    if problems:
        for problem in problems:
            print(f"NOT STARTED: {problem}", file=sys.stderr)
        return 1

    report = await asyncio.to_thread(migrate_v1, source_url=source, target_url=target)
    print("v1 table                          v1 rows   migrated")
    for table, (in_v1, in_v2) in report.items():
        flag = "" if in_v1 == in_v2 else "   <-- MISMATCH"
        print(f"  {table:30} {in_v1:>8} {in_v2:>10}{flag}")

    no_go = [
        f"{table}: {in_v1} in v1, {in_v2} migrated"
        for table, (in_v1, in_v2) in report.discrepancies().items()
    ]
    no_go += await _audit(target, settings.app_role)
    current, fresh, refused = await asyncio.to_thread(
        measurement_parity, source, target
    )
    print(
        f"measurement parity: {current} current in v2 {'=' if current == fresh else '≠'}"
        f" {fresh} fresh in v1"
        f" ({refused} on refused dives: shown by v1, intentionally not current)"
    )
    if current != fresh:
        no_go.append(f"measurement parity: {current} current in v2, {fresh} in v1")

    if no_go:
        print("NO-GO:", file=sys.stderr)
        for reason in no_go:
            print(f"  {reason}", file=sys.stderr)
        return 1
    print("GO: every v1 row accounted for, tenancy audit passed, parity holds")
    return 0


def run() -> None:
    """Console-script entry point."""
    sys.exit(asyncio.run(main(sys.argv[1:])))
