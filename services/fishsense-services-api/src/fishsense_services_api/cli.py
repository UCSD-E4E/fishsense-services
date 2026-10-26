"""``fishsense-services-api`` command line.

fishsense-services-api migrate      # bring the schema to head, as the owner
fishsense-services-api migrate-v1   # one-shot v1 data migration, with go/no-go
fishsense-services-api audit-range-trend --tenant lab 490 491
                                    # read-only calibration audit (range_trend)
fishsense-services-api validate-automatic --tenant <id>
                                    # read-only: the automatic chain against the
                                    # paper's dives (cscw-fishsense2027 §6)
fishsense-services-api add-tenant conservation-angler \\
    --name "Conservation Angler" --org-claim conservation-angler
                                    # a partner's tenant, joined by its org's
                                    # invitees (migration 0037), as the owner
fishsense-services-api backfill-mission-facts --tenant <id> \\
    --ingested-before 2026-10-16T00:00:00+00:00 [--purpose 509=known_length ...]
                                    # dry run: ports, deployments and purposes
                                    # for missions before 0047; --apply <the
                                    # dry run's fingerprint> writes that plan
fishsense-services-api infer-deployments --tenant <id> [--together 398=400 ...]
                                    # dry run: deployments for legacy missions
                                    # from their folders and dates; --apply
                                    # <the dry run's fingerprint> writes them
fishsense-services-api backfill-v1-measurement-inputs --tenant <id>
                                    # dry run: link v1's measurements to the
                                    # depth and labels that reproduce them;
                                    # --apply <fingerprint>, as the owner
fishsense-services-api serve-tenants [--apply]
                                    # dry run: make the orchestrator a member
                                    # of every tenant it isn't yet (add-tenant
                                    # does it for a new one)
fishsense-services-api withdraw-zoomed-lengths [--apply]
                                    # dry run: the automatic lengths on frames
                                    # shot zoomed in (0070); --apply appends a
                                    # `zoomed` refusal after each, as the owner
fishsense-services-api detect-beam-moves [--apply]
                                    # dry run: the missions whose laser moved
                                    # mid-dive (0073); --apply records them, as
                                    # the owner
fishsense-services-api withdraw-moved-lengths [--apply]
                                    # dry run: the automatic lengths after a
                                    # recorded move; --apply appends a
                                    # `beam_moved` refusal after each
fishsense-services-api retire-slate-labels --tenant lab --mission 436 \\
    --label <id> [--label <id> ...] --reason mirrored
                                    # dry run: supersede the slate labels with
                                    # a reason (0077) and withdraw the
                                    # mission's calibration made from them;
                                    # --apply <fingerprint>, as the owner
fishsense-services-api system-admin --user <sub> --grant [--apply]
                                    # dry run: may open any tenant, read-only
                                    # until they unlock it (0056)
fishsense-services-api public-totals --tenant lab --enable [--apply]
                                    # dry run: the signed-out page as it would
                                    # read with the tenant counted (0055)
fishsense-services-api dev-seed --frames deploy/local/ui/frames
                                    # LOCAL ONLY: the UI harness's synthetic
                                    # tenant (compose.ui.yml, dev_seed.py)
"""

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fishsense_services_api import dev_seed
from fishsense_services_api.beam_moves import (
    current_moves,
    detect_beam_move,
    mission_dots,
    record_beam_move,
)
from fishsense_services_api.zoom_gate import (
    withdraw_zoomed_automatic_lengths,
    zoomed_automatic_lengths,
)
from fishsense_services_api.automatic_validation import (
    PAPER_CALIBRATION_SESSIONS,
    PAPER_POOL_MISSIONS,
    PAPER_REEF_MISSIONS,
    PAPER_REEF_FRAMES,
    format_report,
    pool_ladder,
    reef_comparison,
    reef_coverage,
    reef_rejected_given_length,
    table3,
)
from fishsense_services_api.automatic_validation_store import (
    session_calibrations,
    validation_frames,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.public_store import PublicSummary, public_summary
from fishsense_services_api.tenant_slugs import RESERVED_TENANT_SLUGS
from fishsense_services_api.deployment_inference import (
    InferencePlan,
    apply_inference,
    plan_inference,
)
from fishsense_services_api.mission_backfill import (
    BackfillPlan,
    PlanChanged,
    apply_backfill,
    plan_backfill,
)
from fishsense_services_api.mission_facts import PURPOSES
from fishsense_services_api.migrations import (
    current_revision,
    head_revision,
    is_known_revision,
    upgrade,
)
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
from fishsense_services_api.slate_label_retirement import (
    RetirementPlan,
    RetirementRefused,
    apply_retirement,
    plan_retirement,
)
from fishsense_services_api.tenant_stores import TenantStores
from fishsense_services_api.settings import (
    AuditSettings,
    DevSeedSettings,
    MigrationSettings,
    ServiceSettings,
    V1MigrationSettings,
)
from fishsense_services_api.tenant_service import serve, unserved
from fishsense_services_api.v1_measurement_inputs import (
    V1InputsPlan,
    apply_v1_inputs,
    plan_v1_inputs,
)
from fishsense_services_api.v1_migration import (
    measurement_parity,
    migrate_v1,
    preflight,
)

ENV_PREFIX = "FISHSENSE_"
#: One path segment of lowercase letters, digits and dashes: a tenant slug goes
#: into every API path (apps/web lib/env.ts checks the same), and an org is a
#: krg-infra ``collaborator_invites`` key, which has the same shape.
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


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
    audit.add_argument("mission_numbers", type=int, nargs="+", metavar="mission_number")
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
        "--pool-missions",
        "--pool-dives",
        type=int,
        nargs="*",
        default=list(PAPER_POOL_MISSIONS),
    )
    validate.add_argument(
        "--reef-missions",
        "--reef-dives",
        type=int,
        nargs="*",
        default=list(PAPER_REEF_MISSIONS),
    )
    validate.add_argument(
        "--any-reef-frames",
        action="store_true",
        help="score every labelled frame of the reef dives, not only the "
        "paper's 791 (automatic_validation.PAPER_REEF_FRAMES)",
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
    admin = commands.add_parser(
        "system-admin",
        help="grant or revoke a system admin (0056): one who may open any tenant, "
        "read-only until they unlock it; a dry run by default",
    )
    admin.add_argument("--user", required=True, help="their sign-in subject (sub)")
    grant = admin.add_mutually_exclusive_group(required=True)
    grant.add_argument("--grant", action="store_true")
    grant.add_argument("--revoke", action="store_true")
    admin.add_argument("--apply", action="store_true", help="write the change")
    public = commands.add_parser(
        "public-totals",
        help="opt a tenant in or out of the signed-out page's totals and map "
        "(0055); a dry run by default, showing the page as it would read",
    )
    public.add_argument("--tenant", required=True, help="the tenant's slug")
    choice = public.add_mutually_exclusive_group(required=True)
    choice.add_argument("--enable", action="store_true", help="count it")
    choice.add_argument("--disable", action="store_true", help="stop counting it")
    public.add_argument("--apply", action="store_true", help="write the change")
    add_tenant = commands.add_parser(
        "add-tenant",
        help="create (or update) a tenant, optionally joined by a partner org's "
        "accounts, as the schema owner",
    )
    add_tenant.add_argument("slug", help="the tenant's slug, as in its API paths")
    add_tenant.add_argument("--name", required=True, help="its display name")
    add_tenant.add_argument(
        "--org-claim",
        help="the partner org (Authentik `org` claim) whose accounts join this "
        "tenant as members; left as it is when omitted",
    )
    serve_tenants = commands.add_parser(
        "serve-tenants",
        help="make the orchestrator a member of every tenant it isn't yet, as "
        "add-tenant does for a new one; a dry run by default",
    )
    serve_tenants.add_argument("--apply", action="store_true", help="write it")
    withdraw = commands.add_parser(
        "withdraw-zoomed-lengths",
        help="withdraw the automatic lengths on frames shot zoomed in (0070), "
        "appending a refusal after each; a dry run by default",
    )
    withdraw.add_argument("--apply", action="store_true", help="write it")
    detect = commands.add_parser(
        "detect-beam-moves",
        help="find the missions whose laser moved mid-dive (0073) and record "
        "each new move; a dry run by default",
    )
    detect.add_argument("--apply", action="store_true", help="record them")
    moved = commands.add_parser(
        "withdraw-moved-lengths",
        help="withdraw the automatic lengths on frames after a recorded laser "
        "move (0073), appending a refusal after each; a dry run by default",
    )
    moved.add_argument("--apply", action="store_true", help="write it")
    retire = commands.add_parser(
        "retire-slate-labels",
        help="supersede a mission's slate labels with a reason (0077) and "
        "withdraw the calibration made from them; a dry run by default",
    )
    retire.add_argument("--tenant", required=True, help="the tenant's slug, or its id")
    retire.add_argument("--mission", required=True, type=int, help="its number")
    retire.add_argument(
        "--label", action="append", required=True, type=uuid.UUID,
        help="a slate label's id; repeatable",
    )  # fmt: skip
    retire.add_argument(
        "--reason", required=True, help="why, e.g. mirrored; recorded on each label"
    )
    retire.add_argument(
        "--apply",
        metavar="FINGERPRINT",
        help="write the plan whose dry run printed this fingerprint",
    )
    backfill = commands.add_parser(
        "backfill-mission-facts",
        help="plan (and with --apply, write) the port, deployment and purpose of "
        "missions ingested before migration 0047; a dry run by default",
    )
    backfill.add_argument(
        "--tenant", required=True, help="the tenant's slug, or its id"
    )
    backfill.add_argument(
        "--ingested-before",
        required=True,
        type=_aware_datetime,
        help="ISO time with offset: missions created before it are the corrective "
        "optic (the owner's rule, 2026-10-16); later ones need a D-labelled device",
    )
    backfill.add_argument(
        "--purpose",
        action="append",
        default=[],
        metavar="NUMBER=PURPOSE",
        help="override an inferred purpose (or 'none' to leave it unstated); "
        "repeatable, and part of the reviewed plan",
    )
    backfill.add_argument(
        "--apply",
        metavar="FINGERPRINT",
        help="write the plan whose dry run printed this fingerprint",
    )
    infer = commands.add_parser(
        "infer-deployments",
        help="plan (and with --apply, write) deployments for missions in none, "
        "from their folders and dates; a dry run by default",
    )
    infer.add_argument("--tenant", required=True, help="the tenant's slug, or its id")
    infer.add_argument(
        "--gap-days",
        type=_days,
        default=2,
        help="missions at one place at most this many days apart are one trip",
    )
    infer.add_argument(
        "--together",
        action="append",
        default=[],
        metavar="NUMBER=OTHER",
        help="put mission NUMBER on mission OTHER's trip (its date and place); "
        "repeatable, and part of the reviewed plan",
    )
    infer.add_argument(
        "--apply",
        metavar="FINGERPRINT",
        help="write the plan whose dry run printed this fingerprint",
    )
    v1_inputs = commands.add_parser(
        "backfill-v1-measurement-inputs",
        help="plan (and with --apply, write) the depth, laser label and head/tail "
        "label of each v1 measurement, which v1 never recorded; a dry run by "
        "default, as the schema owner",
    )
    v1_inputs.add_argument("--tenant", required=True, help="tenant id or slug")
    v1_inputs.add_argument(
        "--apply",
        metavar="FINGERPRINT",
        help="write the plan whose dry run printed this fingerprint",
    )
    seed = commands.add_parser(
        "dev-seed",
        help="LOCAL ONLY: seed the UI harness's synthetic tenant (compose.ui.yml); "
        "refuses any database not marked as the harness's",
    )
    seed.add_argument(
        "--frames",
        type=Path,
        required=True,
        help="a directory of JPEGs and their frames.json (where each fish is)",
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
    if args.command == "add-tenant":
        return await _add_tenant(args)
    if args.command == "serve-tenants":
        return await _serve_tenants(args)
    if args.command == "withdraw-zoomed-lengths":
        return await _withdraw_lengths(args, "zoomed", "on zoomed frames")
    if args.command == "withdraw-moved-lengths":
        return await _withdraw_lengths(args, "beam_moved", "after a laser move")
    if args.command == "detect-beam-moves":
        return await _detect_beam_moves(args)
    if args.command == "public-totals":
        return await _public_totals(args)
    if args.command == "system-admin":
        return await _system_admin(args)
    if args.command == "backfill-mission-facts":
        return await _backfill_mission_facts(args)
    if args.command == "retire-slate-labels":
        return await _retire_slate_labels(args)
    if args.command == "infer-deployments":
        return await _infer_deployments(args)
    if args.command == "backfill-v1-measurement-inputs":
        return await _backfill_v1_inputs(args)
    if args.command == "dev-seed":
        return await _dev_seed(args)
    return 2  # unreachable: argparse rejects unknown commands


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} has no UTC offset")
    return parsed


def _purpose_overrides(values: Sequence[str]) -> dict[int, str | None] | None:
    overrides: dict[int, str | None] = {}
    for value in values:
        number, _, purpose = value.partition("=")
        if not number.isdigit() or purpose not in (*PURPOSES, "none"):
            print(
                f"--purpose {value!r}: want NUMBER=PURPOSE, PURPOSE one of "
                f"{', '.join(PURPOSES)} or none",
                file=sys.stderr,
            )
            return None
        overrides[int(number)] = None if purpose == "none" else purpose
    return overrides


def format_backfill_plan(plan: BackfillPlan) -> str:
    """The dry run's report: totals first, then every mission it would change."""
    sources = Counter(m.port_source for m in plan.missions if m.port)
    purposes = Counter(m.purpose for m in plan.missions if m.purpose)
    lines = [
        f"ports: {sum(sources.values())} to corrective_optic ("
        + ", ".join(f"{k} {v}" for k, v in sorted(sources.items()))
        + f"); {len(plan.left_unknown)} left unknown",
        "purposes: "
        + (", ".join(f"{k} {v}" for k, v in sorted(purposes.items())) or "none"),
        f"deployments: {len(plan.deployments)}",
    ]
    for trip in plan.deployments:
        state = "exists" if trip.exists else f"new, {trip.started_on}..{trip.ended_on}"
        lines.append(f"  {trip.name}  ({trip.missions} missions; {state})")
        lines.append(f"    {trip.source_path}")
    if plan.left_unknown:
        lines.append(
            "left unknown (no rule decides): " + ", ".join(map(str, plan.left_unknown))
        )
    lines.append("")
    lines.append(
        f"{'mission':>7}  {'port source':<13}  {'purpose':<17}  why / deployment / name"
    )
    for m in plan.missions:
        lines.append(
            f"{m.number:>7}  {m.port_source or '-':<13}  {m.purpose or '-':<17}  "
            f"{m.purpose_why or '-'} / {m.deployment or '-'} / {m.name}"
        )
    return "\n".join(lines)


def format_inference_plan(plan: InferencePlan) -> str:
    """The dry run's report: totals first, then every trip and what to look at."""
    new = [t for t in plan.trips if not t.exists and t.to_write]
    lines = [
        f"trips: {len(plan.trips)}; {len(new)} new deployments; "
        f"{sum(len(t.to_write) for t in plan.trips)} missions to write; "
        f"{sum(t.conflict for t in plan.trips)} left alone",
    ]
    for t in plan.trips:
        state = (
            "LEFT ALONE"
            if t.conflict
            else "exists" if t.exists else f"new, {t.started_on}..{t.ended_on}"
        )
        lines.append(
            f"\n{t.name}  ({state}; {len(t.missions)} missions, {t.sessions} "
            f"sessions; writes {len(t.to_write)})"
        )
        lines.append(f"  missions: {', '.join(map(str, t.missions))}")
        for flag in t.flags:
            lines.append(f"  ! {flag.mission or 'trip'}: {flag.note}")
    return "\n".join(lines)


def _days(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"{value!r}: want a whole number of days")
    return int(value)


def _together(values: Sequence[str]) -> dict[int, int] | None:
    together: dict[int, int] = {}
    for value in values:
        number, _, other = value.partition("=")
        if not (number.isdigit() and other.isdigit()):
            print(f"--together {value!r}: want NUMBER=OTHER", file=sys.stderr)
            return None
        if int(number) in together:
            print(f"--together: mission {number} chosen twice", file=sys.stderr)
            return None
        together[int(number)] = int(other)
    return together


async def _infer_deployments(args: argparse.Namespace) -> int:
    """Dry run unless --apply names the fingerprint of the plan reviewed."""
    together = _together(args.together)
    if together is None:
        return 2
    settings = _settings(AuditSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        tenant_id = await _tenant_id(engine, args.tenant)
        if tenant_id is None:
            print(f"unknown tenant {args.tenant!r}", file=sys.stderr)
            return 1
        if args.apply:
            try:
                async with tenant_transaction(engine, tenant_id) as conn:
                    applied = await apply_inference(
                        conn,
                        tenant_id,
                        expect=args.apply,
                        gap_days=args.gap_days,
                        together=together,
                    )
            except (PlanChanged, ValueError, DBAPIError) as error:
                print(f"nothing written: {error}", file=sys.stderr)
                return 1
            print(
                f"applied {args.apply}: {applied.missions} missions, "
                f"{applied.deployments} new deployments"
            )
            return 0
        try:
            async with tenant_transaction(engine, tenant_id) as conn:
                plan = await plan_inference(
                    conn, tenant_id, gap_days=args.gap_days, together=together
                )
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print("DRY RUN: nothing written")
        print(format_inference_plan(plan))
        again = " ".join(f"--together {v}" for v in args.together)
        print(
            f"\nto write exactly this plan: infer-deployments --tenant {args.tenant} "
            f"--gap-days {args.gap_days} {again + ' ' if again else ''}"
            f"--apply {plan.fingerprint}"
        )
        return 0
    finally:
        await engine.dispose()


async def _backfill_mission_facts(args: argparse.Namespace) -> int:
    """Dry run unless --apply names the fingerprint of the plan reviewed."""
    overrides = _purpose_overrides(args.purpose)
    if overrides is None:
        return 2
    settings = _settings(AuditSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        tenant_id = await _tenant_id(engine, args.tenant)
        if tenant_id is None:
            print(f"unknown tenant {args.tenant!r}", file=sys.stderr)
            return 1
        if args.apply:
            try:
                async with tenant_transaction(engine, tenant_id) as conn:
                    applied = await apply_backfill(
                        conn,
                        tenant_id,
                        ingested_before=args.ingested_before,
                        expect=args.apply,
                        purpose_overrides=overrides,
                    )
            except (PlanChanged, ValueError) as error:
                print(f"nothing written: {error}", file=sys.stderr)
                return 1
            print(
                f"applied {args.apply}: {applied.missions} missions, "
                f"{applied.deployments} new deployments"
            )
            return 0
        try:
            async with tenant_transaction(engine, tenant_id) as conn:
                plan = await plan_backfill(
                    conn,
                    tenant_id,
                    ingested_before=args.ingested_before,
                    purpose_overrides=overrides,
                )
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print("DRY RUN: nothing written")
        print(format_backfill_plan(plan))
        again = " ".join(f"--purpose {v}" for v in args.purpose)
        print(
            f"\nto write exactly this plan: backfill-mission-facts --tenant "
            f"{args.tenant} --ingested-before {args.ingested_before.isoformat()} "
            f"{again + ' ' if again else ''}--apply {plan.fingerprint}"
        )
        return 0
    finally:
        await engine.dispose()


def format_v1_inputs_plan(plan: V1InputsPlan) -> str:
    """The dry run's report: totals, how closely the links reproduce v1, then
    every link that leaves the current results and every skip."""
    leaving = [link for link in plan.links if link.leaves_current]
    lines = [
        f"{len(plan.links)} to link, {len(leaving)} of them leaving the current "
        "results (a linked label is superseded)"
    ]
    if plan.links:
        errors = sorted(link.relative_error for link in plan.links)
        lines.append(
            f"  recomputed vs v1's length, relative: median "
            f"{errors[len(errors) // 2]:.1e}, worst {errors[-1]:.1e}"
        )
    for link in leaving:
        lines.append(f"  leaves current: v1 measurement {link.v1_id}")
    lines.append(f"{len(plan.skipped)} skipped")
    for skip in plan.skipped:
        lines.append(f"  v1 measurement {skip.v1_id}: {skip.reason}")
    return "\n".join(lines)


async def _backfill_v1_inputs(args: argparse.Namespace) -> int:
    """Dry run unless --apply names the fingerprint of the plan reviewed; as
    the schema owner, since the app role may not update `measurements`."""
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        tenant_id = await _tenant_id(engine, args.tenant)
        if tenant_id is None:
            print(f"unknown tenant {args.tenant!r}", file=sys.stderr)
            return 1
        if args.apply:
            try:
                async with tenant_transaction(engine, tenant_id) as conn:
                    linked = await apply_v1_inputs(conn, tenant_id, expect=args.apply)
            except PlanChanged as error:
                print(f"nothing written: {error}", file=sys.stderr)
                return 1
            print(f"applied {args.apply}: linked {linked} v1 measurements")
            return 0
        async with tenant_transaction(engine, tenant_id) as conn:
            plan = await plan_v1_inputs(conn, tenant_id)
        print("DRY RUN: nothing written")
        print(format_v1_inputs_plan(plan))
        print(
            f"\nto write exactly this plan: backfill-v1-measurement-inputs "
            f"--tenant {args.tenant} --apply {plan.fingerprint}"
        )
        return 0
    finally:
        await engine.dispose()


def format_retirement_plan(plan: RetirementPlan) -> str:
    """The dry run's report: what it retires, what it withdraws, what leaves."""
    lines = [f"mission {plan.mission_number}: retire {len(plan.labels)} slate "
             f"labels ({plan.reason}); their points are not changed"]  # fmt: skip
    for label in plan.labels:
        lines.append(
            f"  {label.label_id}  capture {label.capture_number}  "
            f"points {label.reference_points}  skipped {label.skipped_points}"
        )
    if plan.calibration_id is None:
        lines.append("no accepted calibration to withdraw")
    else:
        lines.append(
            f"withdraw calibration {plan.calibration_id} ({plan.calibration_producer})"
            ": a refused one is appended after it"
        )
        lines.append(
            "  borrowed by missions "
            + (", ".join(str(n) for n in plan.borrowers) or "none")
        )
        lines.append(
            f"  {plan.measurements_leaving} current measurement(s) made with it "
            "leave the current set"
        )
    return "\n".join(lines)


async def _retire_slate_labels(args: argparse.Namespace) -> int:
    """Dry run unless --apply names the fingerprint of the plan reviewed; as
    the schema owner."""
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        tenant_id = await _tenant_id(engine, args.tenant)
        if tenant_id is None:
            print(f"unknown tenant {args.tenant!r}", file=sys.stderr)
            return 1
        try:
            if args.apply:
                async with tenant_transaction(engine, tenant_id) as conn:
                    applied = await apply_retirement(
                        conn, tenant_id, args.mission, args.label, args.reason,
                        expect=args.apply,
                    )  # fmt: skip
                print(
                    f"applied {args.apply}: retired {applied.labels} slate labels"
                    + (f"; appended refused calibration {applied.calibration_id}"
                       if applied.calibration_id else "")
                )  # fmt: skip
                return 0
            async with tenant_transaction(engine, tenant_id) as conn:
                plan = await plan_retirement(
                    conn, tenant_id, args.mission, args.label, args.reason
                )
        except (PlanChanged, RetirementRefused) as error:
            print(f"nothing written: {error}", file=sys.stderr)
            return 1
        print("DRY RUN: nothing written")
        print(format_retirement_plan(plan))
        labels = " ".join(f"--label {p.label_id}" for p in plan.labels)
        print(
            f"\nto write exactly this plan: retire-slate-labels --tenant "
            f"{args.tenant} --mission {args.mission} {labels} --reason "
            f"{plan.reason!r} --apply {plan.fingerprint}"
        )
        return 0
    finally:
        await engine.dispose()


async def _system_admin(args: argparse.Namespace) -> int:
    """Dry run unless --apply; guarded on the old value, as the schema owner."""
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    want = bool(args.grant)
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            now = (
                await conn.execute(
                    text("SELECT is_system_admin FROM users WHERE sub = :s"),
                    {"s": args.user},
                )
            ).scalar_one_or_none()
            if now is None:
                print(f"{args.user!r} has not signed in: no such user", file=sys.stderr)
                return 1
            if now == want:
                state = "is" if want else "is not"
                print(f"{args.user} already {state} a system admin", file=sys.stderr)
                return 1
            verb = "grant" if want else "revoke"
            if not args.apply:
                print(
                    f"DRY RUN: nothing written. Would {verb} system admin for "
                    f"{args.user}.\nto write it: system-admin --user {args.user} "
                    f"--{verb} --apply"
                )
                return 0
            changed = await conn.execute(
                text(
                    "UPDATE users SET is_system_admin = :want "
                    "WHERE sub = :s AND is_system_admin = :now"
                ),
                {"s": args.user, "want": want, "now": now},
            )
            if changed.rowcount != 1:
                raise RuntimeError(f"{args.user} changed meanwhile: nothing written")
        print(f"{args.user} {'is now' if want else 'is no longer'} a system admin")
        return 0
    finally:
        await engine.dispose()


async def _serve_tenants(args: argparse.Namespace) -> int:
    """Dry run unless --apply; the apply writes exactly the tenants listed."""
    settings = _settings(MigrationSettings)
    service = _settings(ServiceSettings)
    if settings is None or service is None:
        return 2
    sub = service.orchestrator_sub
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            slugs = await unserved(conn, sub)
            if not slugs:
                print(f"every tenant is served by {sub}: nothing to do")
                return 0
            if not args.apply:
                print(f"DRY RUN: nothing written. Would make {sub} a member of:")
                for slug in slugs:
                    print(f"  {slug}")
                print("\nto write it: serve-tenants --apply")
                return 0
            granted = await serve(conn, sub, slugs)
            if granted != len(slugs):
                raise RuntimeError(
                    f"granted {granted} of {len(slugs)}: tenants changed meanwhile, "
                    "nothing written"
                )
        print(f"{sub} now serves: {', '.join(slugs)}")
        return 0
    finally:
        await engine.dispose()


async def _detect_beam_moves(args: argparse.Namespace) -> int:
    """Dry run unless --apply; the apply records exactly the moves listed."""
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            recorded = await current_moves(conn)
            found = []
            for m in await mission_dots(conn):
                move = detect_beam_move(m.dots)
                if move is None:
                    continue
                if recorded.get(m.mission_id) == (
                    move.moved_after,
                    move.detector_version,
                ):
                    continue
                found.append((m, move))
            if not found:
                print("no new move: nothing to record")
                return 0
            verb = (
                "Recording" if args.apply else "DRY RUN: nothing written. Would record"
            )
            print(f"{verb} {len(found)} move(s):")
            for m, move in found:
                print(f"  mission {m.number}: after {move.moved_after.isoformat()} "
                      f"before {move.moved_before.isoformat()}  dots "
                      f"{move.dots_before}+{move.dots_after}, gain {move.gain}, "
                      f"{move.angle_deg:.2f} deg, {move.apart_px:.0f} px apart")  # fmt: skip
            if not args.apply:
                print("\nto record them: detect-beam-moves --apply")
                return 0
            for m, move in found:
                await record_beam_move(conn, m.tenant_id, m.mission_id, move)
        print(f"recorded {len(found)}")
        return 0
    finally:
        await engine.dispose()


async def _withdraw_lengths(args: argparse.Namespace, reason: str, where: str) -> int:
    """Dry run unless --apply; the apply writes exactly the lengths listed."""
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            listed = await zoomed_automatic_lengths(conn, reason)
            if not listed:
                print(f"no automatic length {where}: nothing to withdraw")
                return 0
            missions = Counter(z.mission_id for z in listed)
            verb = (
                "Withdrawing"
                if args.apply
                else "DRY RUN: nothing written. Would withdraw"
            )
            print(f"{verb} {len(listed)} automatic length(s) {where} "
                  f"in {len(missions)} mission(s):")  # fmt: skip
            if reason == "zoomed":
                focal = Counter(round(z.focal_length_mm, 2) for z in listed)
                print("  by focal length (mm): " + ", ".join(
                    f"{f:g}: {n}" for f, n in sorted(focal.items())))  # fmt: skip
            if not args.apply:
                print(f"\nto write it: {args.command} --apply")
                return 0
            written = await withdraw_zoomed_automatic_lengths(
                conn, [z.measurement_id for z in listed], reason
            )
            if written != len(listed):
                raise RuntimeError(
                    f"wrote {written} of {len(listed)}: lengths changed meanwhile, "
                    "nothing written"
                )
        print(f"withdrew {written}")
        return 0
    finally:
        await engine.dispose()


def format_public_summary(found: PublicSummary) -> str:
    """The signed-out page's numbers, as it would say them."""
    lines = [
        f"{found.lengths:,} lengths: {found.wild_fish:,} wild fish of "
        f"{found.species} species, {found.model_lengths:,} fish-model lengths",
        f"{found.missions:,} missions in {found.deployments:,} deployments",
        "map:" if found.regions else "map: nothing yet",
    ]
    for r in found.regions:
        shown = f"about {r.count:,}" if r.count is not None else f"under {r.under}"
        lines.append(f"  {r.name}: {shown}")
    return "\n".join(lines)


async def _public_totals(args: argparse.Namespace) -> int:
    """Dry run unless --apply: the page as it would read, then nothing written."""
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    want = bool(args.enable)
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            now = (
                await conn.execute(
                    text("SELECT public_totals FROM tenants WHERE slug = :s"),
                    {"s": args.tenant},
                )
            ).scalar_one_or_none()
            if now is None:
                print(f"unknown tenant {args.tenant!r}", file=sys.stderr)
                return 1
            if now == want:
                state = "counts" if want else "does not count"
                print(f"{args.tenant} already {state}: nothing to do", file=sys.stderr)
                return 1
            changed = await conn.execute(
                text(
                    "UPDATE tenants SET public_totals = :want "
                    "WHERE slug = :s AND public_totals = :now"
                ),
                {"s": args.tenant, "want": want, "now": now},
            )
            if changed.rowcount != 1:
                raise RuntimeError(f"{args.tenant} changed meanwhile: nothing written")
            preview = await public_summary(conn)
            if not args.apply:
                await conn.rollback()
                print(
                    f"DRY RUN: nothing written. With {args.tenant} "
                    f"{'in' if want else 'out'}, the public page would say:"
                )
                print(format_public_summary(preview))
                print(
                    f"\nto write it: public-totals --tenant {args.tenant} "
                    f"--{'enable' if want else 'disable'} --apply"
                )
                return 0
        state = "now counts in" if want else "no longer counts in"
        print(f"{args.tenant} {state} the public page. It now says:")
        print(format_public_summary(preview))
        return 0
    finally:
        await engine.dispose()


async def _add_tenant(args: argparse.Namespace) -> int:
    for label, value in (("tenant slug", args.slug), ("org claim", args.org_claim)):
        if value is not None and not SLUG.fullmatch(value):
            print(f"not a valid {label}: {value!r}", file=sys.stderr)
            return 2
    if args.slug in RESERVED_TENANT_SLUGS:
        print(
            f"tenant slug {args.slug!r} is reserved: the web serves tenants under "
            "their slug, and this one is its own route",
            file=sys.stderr,
        )
        return 2
    settings = _settings(MigrationSettings)
    service = _settings(ServiceSettings)
    if settings is None or service is None:
        return 2
    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO tenants (slug, name, org_claim)
                    VALUES (:slug, :name, :org)
                    ON CONFLICT (slug) DO UPDATE SET
                        name = EXCLUDED.name,
                        org_claim = COALESCE(EXCLUDED.org_claim, tenants.org_claim)
                    """),
                {"slug": args.slug, "name": args.name, "org": args.org_claim},
            )
            # Served from the start: the orchestrator runs everything (the
            # owner, 2026-10-21), as a member (tenant_service).
            await serve(conn, service.orchestrator_sub, [args.slug])
    except IntegrityError:
        print(
            f"org {args.org_claim!r} is already claimed by another tenant -- "
            "nothing changed",
            file=sys.stderr,
        )
        return 1
    finally:
        await engine.dispose()
    claim = f", joined by org {args.org_claim}" if args.org_claim else ""
    print(f"tenant {args.slug} ({args.name}){claim}")
    return 0


async def _dev_seed(args: argparse.Namespace) -> int:
    """Everything is checked -- the frames, the stores file, and that the
    database is the harness's -- before anything is written anywhere."""
    settings = _settings(DevSeedSettings)
    if settings is None:
        return 2
    try:
        frames = dev_seed.load_frames(args.frames)
    except (OSError, ValueError, KeyError) as error:
        print(
            f"dev-seed: can't read the frames in {args.frames}: {error}",
            file=sys.stderr,
        )
        return 2
    try:
        configured = dev_seed.load_stores(settings.tenant_stores_file)
    except ValueError as error:
        print(f"dev-seed: {error}", file=sys.stderr)
        return 2
    if dev_seed.TENANT not in configured:
        print(
            f"dev-seed: {settings.tenant_stores_file} has no store for tenant"
            f" {dev_seed.TENANT!r}",
            file=sys.stderr,
        )
        return 2

    engine = create_async_engine(settings.migration_database_url.get_secret_value())
    try:
        async with engine.connect() as conn:
            await dev_seed.assert_dev_database(conn)
        await asyncio.to_thread(
            dev_seed.bootstrap_garage,
            settings.dev_garage_admin_url,
            settings.dev_garage_admin_token.get_secret_value(),
            configured,
        )
        stores = TenantStores(
            endpoint_url=settings.object_store_endpoint_url,
            public_endpoint_url=settings.object_store_public_url,
            region=settings.object_store_region,
            stores=configured,
        )
        report = await dev_seed.seed(engine, frames=frames, stores=stores)
    except dev_seed.NotDevDatabase as error:
        print(f"dev-seed REFUSED: {error}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    if report.created:
        print(
            f"seeded tenant {dev_seed.TENANT}: {report.lite_captures} Lite and"
            f" {report.mobile_captures} mobile captures"
        )
    else:
        print(f"tenant {dev_seed.TENANT} was already seeded -- nothing changed")
    return 0


async def _migrate() -> int:
    settings = _settings(MigrationSettings)
    if settings is None:
        return 2
    database_url = settings.migration_database_url.get_secret_value()
    try:
        at = await current_revision(database_url)
        if at is not None and not is_known_revision(at):
            # A rollback: an older release's migrate on a schema a newer one
            # moved. Migrations are additive, so this code runs on it; failing
            # here would fail the rollback instead (review of v0.6.1).
            print(
                f"WARNING: the database is at revision {at}, newer than this"
                f" release's head {head_revision()} -- a rollback. The schema is"
                " left as it is; downgrade it with the newer release's image if"
                " this code can't run on it (deploy/incus/README.md, Rolling back).",
                file=sys.stderr,
            )
        else:
            await upgrade(database_url, app_role=settings.app_role)
            at = head_revision()
    except Exception as error:  # report, don't traceback: this is a deploy step
        print(f"migration FAILED -- nothing applied: {error}", file=sys.stderr)
        return 1
    print(f"schema at revision {at}")

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
        for number in args.mission_numbers:
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
                pool_missions=args.pool_missions,
                reef_missions=args.reef_missions,
                outputs=outputs,
                label_free=label_free,
                reef_frames=None if args.any_reef_frames else PAPER_REEF_FRAMES,
            )
            sessions = await session_calibrations(
                conn, tenant_id, PAPER_CALIBRATION_SESSIONS
            )
    finally:
        await engine.dispose()
    source = "outputs from " + args.frame_outputs if outputs else "the database"
    source += (
        "; every labelled reef frame"
        if args.any_reef_frames
        else "; the paper's 791 reef frames"
    )
    print(
        format_report(
            source,
            pool_ladder(frames),
            reef_comparison(frames),
            reef_coverage(frames),
            rejected=reef_rejected_given_length(frames),
            calibration=table3(sessions, expected=PAPER_CALIBRATION_SESSIONS),
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
    mission_number: int,
    trends: Mapping[str, RangeTrend | None],
    *,
    min_frames: int,
    min_range_ratio: float,
) -> str:
    """v1's report: one line per object; the note carries the interpretation."""
    lines = ["", f"=== dive {mission_number} ==="]
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
