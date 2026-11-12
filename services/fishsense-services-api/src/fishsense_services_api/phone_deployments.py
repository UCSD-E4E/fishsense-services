"""Every phone mission in a deployment (the owner, 2026-11-11: "everything
should be in a deployment").

A deployment is one field trip. For Lite missions `deployment_inference`
reads it from folder names: one place, on dates at most two days apart (the
owner's rules, 2026-10-20). A phone mission has no folder, so the same rule
reads its place from its ``site`` and its day from its name, which carries the
phone's local day (`phone_missions.mission_name`); ``started_at`` is UTC's.

* As a phone mission is made, it joins the deployment that holds a phone
  mission at the same place within two days -- any phone's, so an iPad and an
  iPhone on one boat are one trip -- or the deployment already named for its
  day and place. Otherwise it starts one, named like the mission
  (``YYYY.MM.DD.FishSense.<place>``). Joining stretches the deployment's dates.
* A mission with no place is a trip of its phone's alone, as an unplaced Lite
  dive is its camera's: named ``…FishSense.Mobile`` (``Mobile 2``, … when the
  day's name is taken). When a place is found later
  (`phone_missions.place_a_phone_mission`), a deployment holding only that
  mission takes the place into its name, if that name is free. Otherwise the
  mission stays where it is: still in a deployment, and no deployment emptied
  (the app role can't delete one).

Missions made before this rule (CCFRP's 16, 2026-11-11) are put in deployments
by ``deploy-phone-missions``: it runs this same code over every phone mission
in none, in the order they started, inside a savepoint it rolls back, and
reports the plan and its fingerprint. Applying re-plans under the tenant's
lock and writes nothing unless the fingerprint is the reviewed one.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_backfill import PlanChanged, plan_fingerprint

__all__ = [
    "GAP_DAYS",
    "Applied",
    "PhoneDeploymentPlan",
    "PlanChanged",
    "PlannedPhoneTrip",
    "apply_phone_deployments",
    "deploy_phone_mission",
    "place_phone_deployment",
    "plan_phone_deployments",
]

#: The Lite rule's gap (`deployment_inference`, ``--gap-days`` 2).
GAP_DAYS = 2
NOTE = "phone missions: one place, dates at most 2 days apart (the owner, 2026-11-11)"
_DAY = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.")
_PLACEHOLDER = r"[.]FishSense[.]Mobile( [0-9]+)?$"


def _day(name: str, started_at: datetime) -> date:
    """The local day the mission's name carries, else UTC's."""
    found = _DAY.match(name or "")
    if found:
        try:
            return date(*map(int, found.groups()))
        except ValueError:
            pass
    return started_at.date()


def _name(day: date, place: str | None) -> str:
    return f"{day:%Y.%m.%d}.FishSense.{place or 'Mobile'}"


async def _lock(conn: AsyncConnection, tenant_id: uuid.UUID) -> None:
    # One tenant's phone deployments are decided one at a time, so two phones
    # syncing at once can't each start the same trip.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"phone-deployments:{tenant_id}"},
    )


async def _free_name(conn, tenant_id, name: str) -> str:
    taken = {
        r.name
        for r in await conn.execute(
            text("""
                SELECT name FROM deployments
                WHERE tenant_id = :t AND (name = :n OR name LIKE :n || ' %')
                """),
            {"t": tenant_id, "n": name},
        )
    }
    found, n = name, 1
    while found in taken:
        n += 1
        found = f"{name} {n}"
    return found


async def deploy_phone_mission(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> uuid.UUID | None:
    """Put a phone mission in its deployment; return it. A mission already in
    one keeps it; a Lite mission is not this rule's (None)."""
    await _lock(conn, tenant_id)
    m = (
        await conn.execute(
            text("""
                SELECT name, site, device_id, started_at, deployment_id
                FROM missions
                WHERE tenant_id = :t AND id = :m AND source = 'mobile'
                """),
            {"t": tenant_id, "m": mission_id},
        )
    ).one_or_none()
    if m is None or m.deployment_id is not None:
        return None if m is None else m.deployment_id
    day = _day(m.name, m.started_at)
    place = (m.site or "").strip() or None
    params = {
        "t": tenant_id,
        "day": day,
        "gap": GAP_DAYS,
        "name": _name(day, place),
        "place": place,
        "device": m.device_id,
    }
    # The same place (or, with none, the same phone) within the gap.
    same_trip = (
        "o.site = :place" if place else "o.site IS NULL AND o.device_id = :device"
    )
    found = (
        await conn.execute(
            text(f"""
                SELECT d.id FROM deployments d
                WHERE d.tenant_id = :t
                  AND ({"d.name = :name OR " if place else ""}(
                      CAST(:day AS date) BETWEEN d.started_on - CAST(:gap AS integer)
                                             AND d.ended_on + CAST(:gap AS integer)
                      AND EXISTS (
                          SELECT 1 FROM missions o
                          WHERE o.tenant_id = d.tenant_id
                            AND o.deployment_id = d.id
                            AND o.source = 'mobile' AND {same_trip})))
                ORDER BY abs(coalesce(d.started_on, CAST(:day AS date))
                             - CAST(:day AS date)), d.number
                LIMIT 1
                """),
            params,
        )
    ).scalar_one_or_none()
    if found is None:
        found = (
            await conn.execute(
                text("""
                    INSERT INTO deployments (tenant_id, name, started_on,
                                             ended_on, notes)
                    VALUES (:t, :name, :day, :day, :note) RETURNING id
                    """),
                {
                    **params,
                    "name": await _free_name(conn, tenant_id, params["name"]),
                    "note": NOTE,
                },
            )
        ).scalar_one()
    else:
        await conn.execute(
            text("""
                UPDATE deployments
                SET started_on = least(coalesce(started_on, :day), :day),
                    ended_on = greatest(coalesce(ended_on, :day), :day)
                WHERE tenant_id = :t AND id = :d
                """),
            {**params, "d": found},
        )
    result = await conn.execute(
        text("""
            UPDATE missions SET deployment_id = :d
            WHERE tenant_id = :t AND id = :m AND deployment_id IS NULL
            """),
        {"t": tenant_id, "m": mission_id, "d": found},
    )
    if result.rowcount != 1:
        raise PlanChanged("the mission changed while being put in its deployment")
    return found


async def place_phone_deployment(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> None:
    """A newly placed mission's placeholder deployment takes the place."""
    await _lock(conn, tenant_id)
    m = (
        await conn.execute(
            text(f"""
                SELECT m.name, m.site, m.started_at, d.id AS deployment_id
                FROM missions m
                JOIN deployments d
                  ON d.tenant_id = m.tenant_id AND d.id = m.deployment_id
                WHERE m.tenant_id = :t AND m.id = :m AND m.source = 'mobile'
                  AND m.site IS NOT NULL AND d.name ~ '{_PLACEHOLDER}'
                  AND NOT EXISTS (
                      SELECT 1 FROM missions o
                      WHERE o.tenant_id = d.tenant_id AND o.deployment_id = d.id
                        AND o.id <> m.id)
                """),
            {"t": tenant_id, "m": mission_id},
        )
    ).one_or_none()
    if m is None:
        return
    name = _name(_day(m.name, m.started_at), m.site.strip())
    await conn.execute(
        text("""
            UPDATE deployments SET name = :n
            WHERE tenant_id = :t AND id = :d
              AND NOT EXISTS (
                  SELECT 1 FROM deployments WHERE tenant_id = :t AND name = :n)
            """),
        {"t": tenant_id, "d": m.deployment_id, "n": name},
    )


@dataclass(frozen=True)
class PlannedPhoneTrip:
    name: str
    #: A deployment that exists already, which the missions join.
    exists: bool
    started_on: date
    ended_on: date
    missions: tuple[int, ...]

    @property
    def missions_count(self) -> int:
        return len(self.missions)


@dataclass(frozen=True)
class PhoneDeploymentPlan:
    trips: tuple[PlannedPhoneTrip, ...]
    fingerprint: str


@dataclass(frozen=True)
class Applied:
    missions: int
    deployments: int


async def _undeployed(conn, tenant_id) -> list[tuple[int, uuid.UUID]]:
    rows = await conn.execute(
        text("""
            SELECT number, id FROM missions
            WHERE tenant_id = :t AND source = 'mobile' AND deployment_id IS NULL
            ORDER BY started_at, number
            FOR UPDATE
            """),
        {"t": tenant_id},
    )
    return [(r.number, r.id) for r in rows]


async def _deploy_all(conn, tenant_id) -> tuple[PlannedPhoneTrip, ...]:
    await _lock(conn, tenant_id)
    before = {
        r.id
        for r in await conn.execute(
            text("SELECT id FROM deployments WHERE tenant_id = :t"), {"t": tenant_id}
        )
    }
    into: dict[uuid.UUID, list[int]] = defaultdict(list)
    for number, mission_id in await _undeployed(conn, tenant_id):
        into[await deploy_phone_mission(conn, tenant_id, mission_id)].append(number)
    if not into:
        return ()
    rows = await conn.execute(
        text("""
            SELECT id, name, started_on, ended_on FROM deployments
            WHERE tenant_id = :t AND id = ANY(:ids)
            """),
        {"t": tenant_id, "ids": list(into)},
    )
    trips = [
        PlannedPhoneTrip(
            name=r.name,
            exists=r.id in before,
            started_on=r.started_on,
            ended_on=r.ended_on,
            missions=tuple(sorted(into[r.id])),
        )
        for r in rows
    ]
    return tuple(sorted(trips, key=lambda t: (t.started_on, t.name)))


def _fingerprint(trips) -> str:
    return plan_fingerprint({"gap_days": GAP_DAYS, "trips": [asdict(t) for t in trips]})


async def plan_phone_deployments(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> PhoneDeploymentPlan:
    """The deployments the tenant's undeployed phone missions would go in.
    Writes nothing: the work is done in a savepoint that is rolled back."""
    savepoint = await conn.begin_nested()
    try:
        trips = await _deploy_all(conn, tenant_id)
    finally:
        await savepoint.rollback()
    return PhoneDeploymentPlan(trips=trips, fingerprint=_fingerprint(trips))


async def apply_phone_deployments(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, expect: str
) -> Applied:
    """Write the reviewed plan, all or nothing, inside the caller's transaction."""
    trips = await _deploy_all(conn, tenant_id)
    found = _fingerprint(trips)
    if found != expect:
        raise PlanChanged(
            f"the plan is now {found}, not the reviewed {expect}: review it again"
        )
    return Applied(
        missions=sum(t.missions_count for t in trips),
        deployments=sum(not t.exists for t in trips),
    )
