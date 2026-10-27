"""The mission-facts backfill (PLAN.md §4.9, Phase 2b): a port, a deployment
and a purpose for what was ingested before migration 0047, tenant-scoped.

**Ports.** The owner's rules (2026-10-16): every mission already ingested is
the corrective optic, and so is any device labeled with the ``D`` suffix.
Nothing else is known, so a mission ingested after the rule with no ``D``
stays ``unknown``. A ``D`` on the mission's path is the stronger evidence and
is recorded as the source (``device_suffix``) whenever it is there; the rest
of what predates ``ingested_before`` is ``backfill``. A port already stated is
never touched.

**Deployments.** Each e4edm dataset folder on a mission's path
(``YYYY.MM.DD.PROJECT.LOCATION``) is a deployment; a deployment of that name
already in the tenant is reused, and a mission already in one is left there.
It starts on the date its name gives. Its end is left unset: mission times
are UTC and the site's zone is not recorded, so an evening dive in San Diego
falls on the next UTC day, and an end date derived from them would be wrong
by a day as often as not.

**Purposes** are inferred from the name (``mission_facts.infer_purpose``) where
none is stated, and an operator may override any of them by mission number --
to a purpose, or to None to leave it unstated. The backfill only fills what
is unstated, so an override naming a mission whose purpose is already stated
is refused rather than silently changing nothing.

A plan is read-only, and its fingerprint covers everything it would write,
the overrides included. Applying takes the fingerprint of the plan that was
reviewed: it re-plans under row locks and writes nothing if the two differ,
and each write is guarded on the exact old values, so a row that moved in
between rolls the whole apply back.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_facts import (
    PURPOSES,
    dataset_date,
    dataset_of,
    device_label_in_path,
    infer_purpose,
)

__all__ = [
    "Applied",
    "BackfillPlan",
    "MissionChange",
    "PlanChanged",
    "PlannedDeployment",
    "apply_backfill",
    "plan_backfill",
    "plan_fingerprint",
]

RULE = "the owner's rule, 2026-10-16"


class PlanChanged(Exception):
    """The tenant's missions are not what the reviewed plan was made from."""


@dataclass(frozen=True)
class PlannedDeployment:
    name: str
    source_path: str
    started_on: date | None
    ended_on: date | None
    #: Already in the tenant: missions join it, and it is not changed.
    exists: bool
    missions: int


@dataclass(frozen=True)
class MissionChange:
    """What the backfill would write to one mission; None leaves a fact as is.
    ``purpose_why`` also says why a purpose was not inferred."""

    number: int
    name: str | None
    source_path: str
    port: str | None
    port_source: str | None
    port_note: str | None
    purpose: str | None
    purpose_why: str | None
    deployment: str | None


@dataclass(frozen=True)
class BackfillPlan:
    missions: tuple[MissionChange, ...]
    deployments: tuple[PlannedDeployment, ...]
    #: Missions ingested since ``ingested_before`` that no rule decides: they
    #: stay ``unknown``, and are listed so the review sees them.
    left_unknown: tuple[int, ...]
    fingerprint: str


@dataclass(frozen=True)
class Applied:
    missions: int
    deployments: int


def _port(row, ingested_before: datetime) -> tuple[str, str, str] | None:
    if row.port != "unknown":
        return None
    label = device_label_in_path(row.source_path)
    if label is not None:
        return (
            "corrective_optic",
            "device_suffix",
            f"device labelled {label} on the mission's path ({RULE}: a D suffix "
            "is the corrective optic)",
        )
    if row.created_at < ingested_before:
        return (
            "corrective_optic",
            "backfill",
            f"ingested before {ingested_before.isoformat()} ({RULE}: every "
            "mission already ingested is the corrective optic)",
        )
    return None


def _purpose(row, overrides: Mapping[int, str | None]) -> tuple[str | None, str | None]:
    if row.purpose is not None:
        return None, None
    if row.number in overrides:
        chosen = overrides[row.number]
        return chosen, "set by the operator" if chosen else None
    return infer_purpose(row.name, row.source_path)


def plan_fingerprint(body: Mapping) -> str:
    """What a reviewed plan is known by: everything it would write, hashed."""
    encoded = json.dumps(body, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _fingerprint(ingested_before, missions, deployments) -> str:
    return plan_fingerprint(
        {
            "ingested_before": ingested_before.isoformat(),
            "missions": [asdict(m) for m in missions],
            "deployments": [asdict(d) for d in deployments],
        }
    )


async def plan_backfill(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    ingested_before: datetime,
    purpose_overrides: Mapping[int, str | None] | None = None,
    _lock: bool = False,
) -> BackfillPlan:
    """What the backfill would write to the tenant's missions. Writes nothing."""
    overrides = dict(purpose_overrides or {})
    unknown = {v for v in overrides.values() if v is not None} - set(PURPOSES)
    if unknown:
        raise ValueError(f"not a purpose: {', '.join(sorted(unknown))}")
    rows = (
        await conn.execute(
            text("""
                SELECT number, name, source_path, port, purpose, deployment_id,
                       created_at, started_at
                FROM missions WHERE tenant_id = :t AND source = 'lite'
                ORDER BY number
                """ + (" FOR UPDATE" if _lock else "")),
            {"t": tenant_id},
        )
    ).all()
    missing = set(overrides) - {r.number for r in rows}
    if missing:
        raise ValueError(f"no such mission: {', '.join(map(str, sorted(missing)))}")
    stated = sorted(
        f"{r.number} ({r.purpose})"
        for r in rows
        if r.number in overrides and r.purpose is not None
    )
    if stated:
        raise ValueError(
            f"purpose already stated for mission {', '.join(stated)}: the backfill "
            "fills only unstated purposes; change these on the mission's page"
        )
    existing = {
        r.name: r.source_path
        for r in await conn.execute(
            text("SELECT name, source_path FROM deployments WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    }

    changes: list[MissionChange] = []
    trips: dict[str, dict] = {}
    left_unknown: list[int] = []
    for row in rows:
        port = _port(row, ingested_before)
        if port is None and row.port == "unknown":
            left_unknown.append(row.number)
        purpose, why = _purpose(row, overrides)
        dataset = dataset_of(row.source_path) if row.deployment_id is None else None
        if dataset is not None:
            trip = trips.setdefault(
                dataset[0],
                {"source_path": existing.get(dataset[0], dataset[1]), "days": []},
            )
            trip["days"].append(row.started_at.date())
        if port is None and purpose is None and dataset is None:
            continue
        changes.append(
            MissionChange(
                number=row.number,
                name=row.name,
                source_path=row.source_path,
                port=port and port[0],
                port_source=port and port[1],
                port_note=port and port[2],
                purpose=purpose,
                purpose_why=why,
                deployment=dataset and dataset[0],
            )
        )
    deployments = tuple(
        PlannedDeployment(
            name=name,
            source_path=trip["source_path"],
            started_on=None if name in existing else dataset_date(name),
            ended_on=None,
            exists=name in existing,
            missions=len(trip["days"]),
        )
        for name, trip in sorted(trips.items())
    )
    missions = tuple(changes)
    return BackfillPlan(
        missions=missions,
        deployments=deployments,
        left_unknown=tuple(left_unknown),
        fingerprint=_fingerprint(ingested_before, missions, deployments),
    )


async def _guarded(conn, sql: str, params: dict) -> None:
    if (await conn.execute(text(sql), params)).rowcount != 1:
        raise PlanChanged(f"mission {params['n']} changed while being written")


async def apply_backfill(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    ingested_before: datetime,
    expect: str,
    purpose_overrides: Mapping[int, str | None] | None = None,
) -> Applied:
    """Write the reviewed plan, all or nothing, inside the caller's transaction."""
    plan = await plan_backfill(
        conn,
        tenant_id,
        ingested_before=ingested_before,
        purpose_overrides=purpose_overrides,
        _lock=True,
    )
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the plan is now {plan.fingerprint}, not the reviewed {expect}: "
            "review it again"
        )
    ids: dict[str, uuid.UUID] = {}
    created = 0
    for trip in plan.deployments:
        if trip.exists:
            ids[trip.name] = (
                await conn.execute(
                    text(
                        "SELECT id FROM deployments WHERE tenant_id = :t AND name = :n"
                    ),
                    {"t": tenant_id, "n": trip.name},
                )
            ).scalar_one()
            continue
        ids[trip.name] = (
            await conn.execute(
                text("""
                    INSERT INTO deployments (tenant_id, name, source_path,
                                             started_on, ended_on)
                    VALUES (:t, :n, :p, :s, :e) RETURNING id
                    """),
                {
                    "t": tenant_id,
                    "n": trip.name,
                    "p": trip.source_path,
                    "s": trip.started_on,
                    "e": trip.ended_on,
                },
            )
        ).scalar_one()
        created += 1
    for change in plan.missions:
        key = {"t": tenant_id, "n": change.number}
        if change.port is not None:
            await _guarded(
                conn,
                """
                UPDATE missions SET port = :port, port_source = :source,
                                    port_note = :note
                WHERE tenant_id = :t AND number = :n
                  AND port = 'unknown' AND port_source IS NULL
                """,
                {
                    **key,
                    "port": change.port,
                    "source": change.port_source,
                    "note": change.port_note,
                },
            )
        if change.purpose is not None:
            await _guarded(
                conn,
                "UPDATE missions SET purpose = :purpose "
                "WHERE tenant_id = :t AND number = :n AND purpose IS NULL",
                {**key, "purpose": change.purpose},
            )
        if change.deployment is not None:
            await _guarded(
                conn,
                "UPDATE missions SET deployment_id = :d "
                "WHERE tenant_id = :t AND number = :n AND deployment_id IS NULL",
                {**key, "d": ids[change.deployment]},
            )
    return Applied(missions=len(plan.missions), deployments=created)
