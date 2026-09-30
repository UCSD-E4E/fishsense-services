"""The post-converge smoke test: GO or NO-GO (PLAN.md §6.6 step 6).

    docker compose ... run --rm smoke --dive 490 [--min-measurements N]

(docs/cutover.md has the full invocation on the slot.) New in v2. v1 had no
such gate: `deploy.yml`'s green meant "the converge fired", and its verify job
compared image pins only (fishsense-lite deploy.yml `verify-incus`), so a
deploy whose containers were up but whose schema, schedules or credentials
were wrong read as success. Before the portal reopens the cutover needs one
answer, so this prints a PASS/FAIL line per check and exits 0 only when every
check passed:

* the API's ``/healthz``, and its OpenAPI document (the web's client is
  generated from it);
* the database at the migration head, and the tenancy audit clean -- as the
  schema owner, which is how ``migrate`` checks both;
* the lab tenant present (``migrate-v1`` creates it);
* a known dive's measurements readable **as the research role**, through the
  ``v1`` views the research repos query (migration 0031): the one check that
  reads as those consumers do (PLAN.md §2.7). A dive number is required;
* Temporal reachable over mTLS, with every schedule the orchestrator and the
  backup ensure present, and none of v1's left (step 5 deletes them: v2's
  scheduled workflows share v1's class names and minute offsets, so a v1
  schedule left running collides with v2's workflow ids);
* Label Studio reachable with the orchestrator's key, and its workspace found;
* the web's landing page;
* the object store readable: a key under v1's JPEG prefix, which every migrated
  Label Studio task points at.

Runs on the orchestrator's image, inside the interior network, with the
orchestrator's settings plus the owner's DSN and a research login's (the
``smoke`` compose service). Every check is bounded by a timeout and isolated,
so one hung dependency is reported rather than hanging the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "CHECK_NAMES",
    "NotConfigured",
    "Result",
    "SmokeOptions",
    "SmokeSettings",
    "expected_schedule_ids",
    "main",
    "parse_args",
    "print_report",
    "run_checks",
    "verdict",
]

#: Per check. Generous: a cold TLS handshake to krg-prod plus a schedule
#: listing, or Label Studio's rate limiter, takes seconds, not minutes.
DEFAULT_TIMEOUT_SECONDS: Final = 60.0

#: v1's schedule ids (fishsense-lite@origin/main services/*: every
#: `ensure_schedule` call). Any of them still on the shared namespace after
#: the switch is NO-GO -- see the module docstring.
V1_SCHEDULE_IDS: Final = frozenset(
    {
        "cluster-dive-frames-workflow-schedule",
        "compute-laser-depths-workflow-schedule",
        "evaluate-laser-auto-accept-workflow-schedule",
        "measure-fish-workflow-schedule",
        "perform-checkerboard-calibration-workflow-schedule",
        "perform-laser-calibration-workflow-schedule",
        "populate-headtail-labels-workflow-schedule",
        "populate-laser-labels-workflow-schedule",
        "populate-species-labels-workflow-schedule",
        "predict-headtail-images-workflow-schedule",
        "predict-laser-images-workflow-schedule",
        "predict-slate-images-workflow-schedule",
        "preprocess-headtail-images-workflow-schedule",
        "preprocess-laser-images-workflow-schedule",
        "preprocess-slate-images-workflow-schedule",
        "preprocess-species-images-workflow-schedule",
        "reconcile-labeling-configs-workflow-schedule",
        "scale-down-idle-data-worker-workflow-schedule",
        "sync-label-studio-dive-slate-labels-workflow-schedule",
        "sync-label-studio-headtail-labels-workflow-schedule",
        "sync-label-studio-laser-labels-workflow-schedule",
        "sync-label-studio-species-labels-workflow-schedule",
        "fishsense-daily-db-backup",
    }
)


class NotConfigured(RuntimeError):
    """A probe has no credential to run with. A failure, never a skip."""


class SmokeFailure(RuntimeError):
    """A check ran and found the slot wrong."""


class SmokeSettings(BaseSettings):
    """``FISHSENSE_SMOKE_*``: where the smoke looks. The rest of what it needs
    is the orchestrator's own configuration (Temporal, Label Studio, the
    object store) and the owner's DSN (``FISHSENSE_MIGRATION_DATABASE_URL``)."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_SMOKE_")

    #: The interior hosts: the public edge failing is the edge's problem, and
    #: would read as ours.
    api_url: str = "http://api:8000"
    web_url: str = "http://web:3000"
    #: A login in `fishsense_research` (the bootstrap's `fishsense_smoke`).
    research_database_url: SecretStr | None = None


@dataclass(frozen=True)
class SmokeOptions:
    """What the checks compare against: settings plus the command line."""

    api_url: str
    web_url: str
    label_studio_workspace: str
    dive_number: int
    min_measurements: int = 1


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str


class Probes(Protocol):
    """The live boundary: everything a check reads from the slot."""

    async def http_get(self, url: str) -> tuple[int, bytes]: ...

    async def migration_revision(self) -> str | None: ...

    async def tenancy_violations(self) -> list[str]: ...

    async def lab_tenant_id(self) -> uuid.UUID | None: ...

    async def research_measurement_count(self, dive_number: int) -> int: ...

    async def temporal_schedule_ids(self) -> set[str]: ...

    async def label_studio_workspace_id(self, name: str) -> int | None: ...

    async def object_store_sample(self) -> str | None: ...


def expected_schedule_ids() -> set[str]:
    """Every schedule the orchestrator ensures at startup, and the backup's."""
    # pylint: disable=import-outside-toplevel
    from fishsense_services_orchestrator.ops.backup.settings import BackupSettings
    from fishsense_services_orchestrator.schedules import SCHEDULES

    return {s.schedule_id for s in SCHEDULES} | {
        BackupSettings.model_fields["schedule_id"].default
    }


# --- the checks -----------------------------------------------------------------


async def _page(p: Probes, url: str) -> tuple[int, bytes]:
    status, body = await p.http_get(url)
    if status != 200:
        raise SmokeFailure(f"GET {url} -> {status}")
    return status, body


async def _api_healthz(p: Probes, o: SmokeOptions) -> str:
    await _page(p, f"{o.api_url}/healthz")
    return "200"


async def _api_openapi(p: Probes, o: SmokeOptions) -> str:
    _, body = await _page(p, f"{o.api_url}/openapi.json")
    document = json.loads(body)
    if "openapi" not in document or not document.get("paths"):
        raise SmokeFailure("served, but not an OpenAPI document with paths")
    return f"OpenAPI {document['openapi']}, {len(document['paths'])} paths"


async def _db_at_head(p: Probes, _o: SmokeOptions) -> str:
    # pylint: disable=import-outside-toplevel
    from fishsense_services_api.migrations import head_revision

    head, at = head_revision(), await p.migration_revision()
    if at != head:
        raise SmokeFailure(f"schema at {at or 'nothing'}, head is {head}")
    return f"at {head}"


async def _tenancy_audit(p: Probes, _o: SmokeOptions) -> str:
    violations = await p.tenancy_violations()
    if violations:
        raise SmokeFailure("; ".join(violations))
    return "clean"


async def _lab_tenant(p: Probes, _o: SmokeOptions) -> str:
    lab = await p.lab_tenant_id()
    if lab is None:
        raise SmokeFailure("no tenant with slug 'lab' -- has migrate-v1 run?")
    return str(lab)


async def _research_measurements(p: Probes, o: SmokeOptions) -> str:
    count = await p.research_measurement_count(o.dive_number)
    if count < o.min_measurements:
        raise SmokeFailure(
            f"dive {o.dive_number}: {count} measurements through v1.measurement, "
            f"expected at least {o.min_measurements}"
        )
    return f"dive {o.dive_number}: {count} measurements"


async def _temporal_schedules(p: Probes, _o: SmokeOptions) -> str:
    listed = await p.temporal_schedule_ids()
    missing = sorted(expected_schedule_ids() - listed)
    leftover = sorted(V1_SCHEDULE_IDS & listed)
    problems = []
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    if leftover:
        problems.append(f"v1's still present (delete them): {', '.join(leftover)}")
    if problems:
        raise SmokeFailure("; ".join(problems))
    return f"{len(listed)} schedules, all of v2's"


async def _label_studio(p: Probes, o: SmokeOptions) -> str:
    workspace = await p.label_studio_workspace_id(o.label_studio_workspace)
    if workspace is None:
        raise SmokeFailure(f"workspace {o.label_studio_workspace!r} not found")
    return f"workspace {o.label_studio_workspace!r} = {workspace}"


async def _web(p: Probes, o: SmokeOptions) -> str:
    await _page(p, f"{o.web_url}/")
    return "200"


async def _object_store(p: Probes, _o: SmokeOptions) -> str:
    key = await p.object_store_sample()
    if key is None:
        raise SmokeFailure("readable, but nothing under v1's JPEG prefix")
    return f"read {key}"


Check = Callable[[Probes, SmokeOptions], Awaitable[str]]

_CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("api healthz", _api_healthz),
    ("api openapi", _api_openapi),
    ("db at migration head", _db_at_head),
    ("tenancy audit", _tenancy_audit),
    ("lab tenant", _lab_tenant),
    ("research reads the dive's measurements", _research_measurements),
    ("temporal schedules", _temporal_schedules),
    ("label studio", _label_studio),
    ("web", _web),
    ("object store", _object_store),
)
CHECK_NAMES: Final = tuple(name for name, _ in _CHECKS)


async def _one(
    name: str, check: Check, p: Probes, o: SmokeOptions, timeout: float
) -> Result:
    try:
        detail = await asyncio.wait_for(check(p, o), timeout)
    except TimeoutError:
        return Result(name, False, f"no answer in {timeout:g}s")
    except Exception as error:  # pylint: disable=broad-except
        # Every failure is a result, never a traceback: the gate must report
        # all ten, and an exception's type is often the whole diagnosis.
        return Result(name, False, f"{type(error).__name__}: {error}")
    return Result(name, True, detail)


async def run_checks(
    probes: Probes,
    options: SmokeOptions,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Result]:
    """Every check, in the runbook's order, each isolated and bounded."""
    return [
        await _one(name, check, probes, options, timeout_seconds)
        for name, check in _CHECKS
    ]


def verdict(results: Iterable[Result]) -> int:
    """0 (GO) only when every check passed."""
    return 0 if all(r.ok for r in results) else 1


def print_report(results: Sequence[Result]) -> None:
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}: {r.detail}")
    failed = sum(not r.ok for r in results)
    if failed:
        print(f"NO-GO: {failed} of {len(results)} checks failed")
    else:
        print(f"GO: all {len(results)} checks passed")


# --- the live probes --------------------------------------------------------------


class LiveProbes:  # pylint: disable=too-many-instance-attributes
    """The slot, reached with the smoke service's configuration."""

    def __init__(self) -> None:
        # pylint: disable=import-outside-toplevel
        from fishsense_services_api.settings import MigrationSettings

        self._smoke = SmokeSettings()
        self._migration = MigrationSettings()

    async def http_get(self, url: str) -> tuple[int, bytes]:
        def _get() -> tuple[int, bytes]:
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        return await asyncio.to_thread(_get)

    async def _owner_scalar(self, sql: str):
        # pylint: disable=import-outside-toplevel
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            self._migration.migration_database_url.get_secret_value()
        )
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text(sql))).scalar_one_or_none()
        finally:
            await engine.dispose()

    async def migration_revision(self) -> str | None:
        return await self._owner_scalar(
            "SELECT version_num FROM alembic_version "
            "WHERE to_regclass('alembic_version') IS NOT NULL"
        )

    async def tenancy_violations(self) -> list[str]:
        # pylint: disable=import-outside-toplevel
        from sqlalchemy.ext.asyncio import create_async_engine

        from fishsense_services_api.schema_audit import tenancy_violations

        engine = create_async_engine(
            self._migration.migration_database_url.get_secret_value()
        )
        try:
            async with engine.connect() as conn:
                return await tenancy_violations(conn, app_role=self._migration.app_role)
        finally:
            await engine.dispose()

    async def lab_tenant_id(self) -> uuid.UUID | None:
        # The owner bypasses RLS (the bootstrap makes it BYPASSRLS, as
        # migrate-v1 requires), so `tenants` shows it every row.
        return await self._owner_scalar("SELECT id FROM tenants WHERE slug = 'lab'")

    async def research_measurement_count(self, dive_number: int) -> int:
        # pylint: disable=import-outside-toplevel
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        if self._smoke.research_database_url is None:
            raise NotConfigured("FISHSENSE_SMOKE_RESEARCH_DATABASE_URL is not set")
        engine = create_async_engine(
            self._smoke.research_database_url.get_secret_value()
        )
        try:
            async with engine.connect() as conn:
                # v1's shape, as imwut's and cscw's extracts join it.
                return (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM v1.measurement m "
                            "JOIN v1.image i ON i.id = m.image_id "
                            "WHERE i.dive_id = :dive AND m.length_m IS NOT NULL"
                        ),
                        {"dive": dive_number},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    async def temporal_schedule_ids(self) -> set[str]:
        # pylint: disable=import-outside-toplevel
        from temporalio.client import Client

        from fishsense_services_contracts.temporal import connect_options
        from fishsense_services_orchestrator.settings import TemporalSettings

        client = await Client.connect(**connect_options(TemporalSettings()))
        return {s.id async for s in await client.list_schedules()}

    async def label_studio_workspace_id(self, name: str) -> int | None:
        # pylint: disable=import-outside-toplevel
        from fishsense_services_orchestrator.labels.label_studio import (
            LabelStudioClient,
            LabelStudioSettings,
        )

        client = LabelStudioClient.from_settings(LabelStudioSettings())
        return await client.workspace_id(name)

    async def object_store_sample(self) -> str | None:
        # pylint: disable=import-outside-toplevel
        from fishsense_services_contracts.object_store import ObjectStoreConnection
        from fishsense_services_orchestrator.object_store.store import (
            build_s3_client,
        )

        settings = ObjectStoreConnection()
        prefix = settings.legacy_labels_prefix
        s3 = build_s3_client(settings)

        def _first() -> str | None:
            listing = s3.list_objects_v2(
                Bucket=settings.labels_bucket,
                Prefix=f"{prefix}/" if prefix else "",
                MaxKeys=1,
            )
            contents = listing.get("Contents") or []
            return contents[0]["Key"] if contents else None

        return await asyncio.to_thread(_first)


# --- the command line -------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m fishsense_services_orchestrator.ops.smoke",
        description="Post-converge smoke test: exit 0 is GO, 1 is NO-GO.",
    )
    parser.add_argument(
        "--dive",
        type=int,
        required=True,
        help="a migrated dive's number (v1's id) with known measurements",
    )
    parser.add_argument(
        "--min-measurements",
        type=int,
        default=1,
        help="fewest measurements the research role must read for it",
    )
    return parser.parse_args(argv)


def _live_probes() -> Probes:
    return LiveProbes()


def _options(args: argparse.Namespace) -> SmokeOptions:
    # pylint: disable=import-outside-toplevel
    from fishsense_services_orchestrator.labels.label_studio import (
        LabelStudioSettings,
    )

    settings = SmokeSettings()
    return SmokeOptions(
        api_url=settings.api_url.rstrip("/"),
        web_url=settings.web_url.rstrip("/"),
        label_studio_workspace=LabelStudioSettings().workspace,
        dive_number=args.dive,
        min_measurements=args.min_measurements,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    results = asyncio.run(run_checks(_live_probes(), _options(args)))
    print_report(results)
    return verdict(results)


if __name__ == "__main__":
    sys.exit(main())
