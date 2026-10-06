"""``fishsense-services-api`` command line.

fishsense-services-api migrate      # bring the schema to head, as the owner
fishsense-services-api migrate-v1   # one-shot v1 data migration, with go/no-go
fishsense-services-api audit-range-trend --tenant lab 490 491
                                    # read-only calibration audit (range_trend)
fishsense-services-api validate-automatic --tenant <id>
                                    # read-only: the automatic chain against the
                                    # paper's dives (cscw-fishsense2027 §6)
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Mapping, Sequence

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fishsense_services_api.automatic_validation import (
    PAPER_POOL_DIVES,
    PAPER_REEF_DIVES,
    format_report,
    pool_ladder,
    reef_comparison,
    reef_coverage,
)
from fishsense_services_api.automatic_validation_store import validation_frames
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.migrations import head_revision, upgrade
from fishsense_services_api.range_trend import (
    DEFAULT_MIN_DEPTH_M,
    DEFAULT_MIN_FRAMES,
    DEFAULT_MIN_RANGE_RATIO,
    RangeTrend,
    group_by_object,
    range_trend,
)
from fishsense_services_api.range_trend_store import (
    RangeTrendInputs,
    range_trend_inputs,
)
from fishsense_services_api.schema_audit import tenancy_violations
from fishsense_services_api.settings import (
    AuditSettings,
    MigrationSettings,
    V1MigrationSettings,
)
from fishsense_services_api.v1_migration import (
    measurement_parity,
    migrate_v1,
    preflight,
)

ENV_PREFIX = "FISHSENSE_"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishsense-services-api")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="migrate the schema to head, as its owner")
    commands.add_parser(
        "migrate-v1", help="migrate v1's data into the lab tenant, then validate"
    )
    audit = commands.add_parser(
        "audit-range-trend",
        help="audit dives' laser calibrations by their rigid objects' range "
        "trend (read-only)",
    )
    audit.add_argument("--tenant", required=True, help="the tenant's slug, or its id")
    audit.add_argument("dive_numbers", type=int, nargs="+", metavar="dive_number")
    audit.add_argument("--min-frames", type=int, default=DEFAULT_MIN_FRAMES)
    audit.add_argument("--min-range-ratio", type=float, default=DEFAULT_MIN_RANGE_RATIO)
    validate = commands.add_parser(
        "validate-automatic",
        help="score the automatic chain against human labels and tape on the "
        "paper's dives, by its metrics (read-only)",
    )
    validate.add_argument(
        "--tenant", required=True, help="the tenant's slug, or its id"
    )
    validate.add_argument(
        "--pool-dives", type=int, nargs="*", default=list(PAPER_POOL_DIVES)
    )
    validate.add_argument(
        "--reef-dives", type=int, nargs="*", default=list(PAPER_REEF_DIVES)
    )
    validate.add_argument(
        "--frame-outputs",
        help="JSON lines of automatic outputs by capture number, replacing the "
        "database's (capture_number, auto_dot, auto_head_tail, "
        "auto_head_tail_humandot)",
    )
    validate.add_argument(
        "--label-free",
        help="JSON {dive number: {laser_position, laser_axis}}, replacing the "
        "database's label-free calibrations",
    )
    return parser


async def main(argv: Sequence[str]) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "migrate":
        return await _migrate()
    if args.command == "migrate-v1":
        return await _migrate_v1()
    if args.command == "audit-range-trend":
        return await _audit_range_trend(args)
    if args.command == "validate-automatic":
        return await _validate_automatic(args)
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
    for table, reasons in report.skipped.items():
        for reason, n in reasons.items():
            print(f"skipped {table}: {n} {reason} (v2 refuses it; not migrated)")

    no_go = [
        f"{table}: {in_v1} in v1, {in_v2} migrated"
        for table, (in_v1, in_v2) in report.discrepancies().items()
    ]
    no_go += await _audit(target, settings.app_role)
    current, fresh, refused, stale = await asyncio.to_thread(
        measurement_parity, source, target
    )
    print(
        f"measurement parity: {current} current in v2 {'=' if current == fresh else '≠'}"
        f" {fresh} fresh in v1"
        f" ({refused} on refused dives: shown by v1, intentionally not current;"
        f" {stale} stale bindings: deleted by v1's next measure run, not current)"
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


async def _audit_range_trend(args: argparse.Namespace) -> int:
    """v1's scripts/audit_length_range_trend.py (fishsense-lite@77e8f8e5).

    Read-only. Exits 0 when every dive was found, whatever it reports: a
    flag is a finding for a person to read, not a failure of the command.
    """
    settings = _settings(AuditSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        tenant_id = await _tenant_id(engine, args.tenant)
        if tenant_id is None:
            print(
                f"unknown tenant {args.tenant!r} (a role under RLS sees a tenant "
                "only by its id: pass --tenant <id>)",
                file=sys.stderr,
            )
            return 1
        missing = False
        for number in args.dive_numbers:
            async with tenant_transaction(engine, tenant_id) as conn:
                inputs = await range_trend_inputs(conn, tenant_id, number)
            if inputs is None:
                print(f"dive {number}: no such dive in the tenant", file=sys.stderr)
                missing = True
                continue
            if inputs.baseline_m is None:
                print(f"dive {number}: no resolvable laser extrinsics", file=sys.stderr)
            print(
                format_range_trend_report(
                    number,
                    _range_trends(inputs, args.min_frames, args.min_range_ratio),
                    min_frames=args.min_frames,
                    min_range_ratio=args.min_range_ratio,
                )
            )
        return 1 if missing else 0
    finally:
        await engine.dispose()


async def _validate_automatic(args: argparse.Namespace) -> int:
    """The validation harness: the paper's tables from a database. Read-only;
    exits 0 whatever the numbers say (they are findings, not failures)."""
    settings = _settings(AuditSettings)
    if settings is None:
        return 2
    outputs = label_free = None
    if args.frame_outputs:
        with open(args.frame_outputs, encoding="utf-8") as fh:
            outputs = {
                int(r["capture_number"]): r
                for r in (json.loads(line) for line in fh if line.strip())
            }
    if args.label_free:
        with open(args.label_free, encoding="utf-8") as fh:
            label_free = {
                int(k): (v["laser_position"], v["laser_axis"])
                for k, v in json.load(fh).items()
            }
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        tenant_id = await _tenant_id(engine, args.tenant)
        if tenant_id is None:
            print(f"unknown tenant {args.tenant!r}", file=sys.stderr)
            return 1
        async with tenant_transaction(engine, tenant_id) as conn:
            frames = await validation_frames(
                conn,
                tenant_id,
                pool_dives=args.pool_dives,
                reef_dives=args.reef_dives,
                outputs=outputs,
                label_free=label_free,
            )
    finally:
        await engine.dispose()
    source = "outputs from " + args.frame_outputs if outputs else "the database"
    print(
        format_report(
            source, pool_ladder(frames), reef_comparison(frames), reef_coverage(frames)
        )
    )
    return 0


async def _tenant_id(engine: AsyncEngine, tenant: str) -> uuid.UUID | None:
    """The tenant by id or slug. RLS shows the app role a tenant only inside
    that tenant's scope, so an id is checked there; a slug resolves only for
    a role that can read `tenants` (the owner)."""
    try:
        tenant_id = uuid.UUID(tenant)
    except ValueError:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT id FROM tenants WHERE slug = :slug"),
                    {"slug": tenant},
                )
            ).scalar_one_or_none()
    async with tenant_transaction(engine, tenant_id) as conn:
        return (
            await conn.execute(
                text("SELECT id FROM tenants WHERE id = :id"), {"id": tenant_id}
            )
        ).scalar_one_or_none()


def _range_trends(
    inputs: RangeTrendInputs, min_frames: int, min_range_ratio: float
) -> dict[str, RangeTrend | None]:
    """v1's `audit_dive`: the range trend per object; None where the data
    cannot support a slope, and nothing without a resolvable calibration."""
    if inputs.baseline_m is None:
        return {}
    groups = group_by_object(
        inputs.measurements, inputs.depth_by_capture, inputs.name_by_capture
    )
    return {
        name: range_trend(
            zs,
            ls,
            inputs.baseline_m,
            min_frames=min_frames,
            min_range_ratio=min_range_ratio,
        )
        for name, (zs, ls) in sorted(groups.items())
    }


def format_range_trend_report(
    dive_number: int,
    trends: Mapping[str, RangeTrend | None],
    *,
    min_frames: int,
    min_range_ratio: float,
) -> str:
    """v1's report: one line per object; the note carries the interpretation."""
    lines = ["", f"=== dive {dive_number} ==="]
    if not trends:
        lines.append("  no measured rigid objects")
        return "\n".join(lines)
    lines.append(
        f"  {'object':<16} {'n':>3} {'range (m)':>11} {'slope %/m':>10} "
        f"{'95% CI':>17} {'angle':>8}  note"
    )
    for name, t in trends.items():
        if t is None:
            lines.append(
                f"  {name:<16} insufficient (need >= {min_frames} frames "
                f"beyond {DEFAULT_MIN_DEPTH_M} m spanning >= {min_range_ratio}x)"
            )
            continue
        flag = "FLAG " if t.flagged else "     "
        zlo, zhi = t.depth_range_m
        lo, hi = t.ci_pct_per_m
        lines.append(
            f"  {name:<16} {t.n:>3} {zlo:4.2f}-{zhi:4.2f} "
            f"{t.slope_pct_per_m:>+10.2f} [{lo:+6.2f},{hi:+6.2f}] "
            f"{t.eps_deg:>+7.3f}d  {flag}{t.note}"
        )
    return "\n".join(lines)


def run() -> None:
    """Console-script entry point."""
    sys.exit(asyncio.run(main(sys.argv[1:])))
