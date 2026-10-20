"""Deployments inferred for legacy missions from their folders and dates (PLAN.md
§4.9; the owner, 2026-10-20).

The mission-facts backfill (`mission_backfill`) groups what sits in an e4edm
dataset folder. The rest -- ~490 of production's 525 missions, copied from the
field into NAS dumps (``2023-09-07 REEF Data Dump``, ``2024.06.20.REEF``,
``2025-02-10 REEF Data Dump SMILE 6``, ``drive-download-…``) -- belong to no
recorded trip. Their folders say when and where, in the field team's own
words, and the owner's rules for reading them (2026-10-20) are:

* **A deployment is one place on dates at most ``gap_days`` apart** (2 by
  default), so a two-day trip is one deployment and the next month's is
  another.
* **The folder's date beats the camera's clock**: file names are what the
  team wrote, and the clocks drift -- FSL-06's ``102724_Islamorada`` frames are
  dated 2023-10-27, a year out. The nearest folder with a date code decides
  (``MMDDYY``, ``M.D.YYYY``, ``MM_DD_YY``, an e4edm dataset's name). A dump's
  own date is when it was copied, and is written in none of those forms
  (``2023-09-07``, ``2024.06.20.REEF``, ``20240307T1050Z``), so it is never read. A code that is no date
  (``082329``) or could be two (``11723``) is not read, nor is the folder
  above it: the last capture's date decides instead. Capture times are the
  camera's local time stored as UTC, so their UTC date is the dive's.
* **The nearest folder naming a place decides** (`PLACES`): a site's name
  places it in its town. A session with no place joins the trip its camera
  was on within the gap; otherwise it is named by its date alone.
* **Copies are one session.** The same frames filed in several dumps (one
  camera, same first and last frame, same count) share a place and a date any
  copy has, and go to the same deployment.
* **An operator can put a mission with another** (``together``): it takes that
  mission's date and place. ``082024_Dive1_FSL05_fix site`` was dived beside
  FSL-01's ``070924_Dive1`` (the same minutes, two cameras), not on 08-20.

A trip holding a mission already in a deployment joins it, and that
deployment is not changed; a trip spanning two is left alone and said. A new
deployment is named like e4edm's (``YYYY.MM.DD.FishSense.Place``) with no
source path, since no folder is the trip's.

A plan is read-only, and its fingerprint covers everything it would write and
the operator's choices. Applying takes the fingerprint of the plan that was
reviewed: it re-plans under row locks and writes nothing if the two differ,
and each write is guarded on the exact old value, so a row that moved in
between rolls the whole apply back.
"""

import hashlib
import json
import re
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_facts import dataset_date

__all__ = [
    "Applied",
    "Flag",
    "InferencePlan",
    "PLACES",
    "PlanChanged",
    "PlannedTrip",
    "apply_inference",
    "folder_date",
    "place_of",
    "plan_inference",
]

#: A place, and the words in a folder's name that put it there; the first match
#: in the nearest folder that has one wins.
PLACES = (
    ("Nathan's Pool", r"Nathans? Pool"),
    ("Pool", r"Pool (Calibration|Validation)|Fish Deg(ree)? Angle"),
    ("Cayman Islands", r"Cayman"),
    ("Cuba", r"Cuba"),
    ("St Lucia", r"St\.? ?Lucia"),
    ("Curaçao", r"Cura[cç]ao"),
    ("Qui Lagoon", r"Qui Lagoon"),
    ("San Diego", r"San ?Diego"),
    ("Key Largo", r"Key ?Largo|Molasses|Mol(HITW|PeLe)|Conch|Snapper ?Ledge|"
                  r"Pompano|Elbow"),  # fmt: skip
    ("Islamorada", r"Islamorada|Alligator|Davis ?Reef|Dav[ey]{1,2} ?Crock|"
                   r"Rocky ?Top|Chutes|Victory|Hens ?Chicks|Cheeca"),  # fmt: skip
)
_PLACES = tuple((name, re.compile(rx, re.I)) for name, rx in PLACES)

_DATASET = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.[^.]+\.\S.*$")
_SIX = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)")
_FIVE = re.compile(r"(?<!\d)\d{5}(?!\d)")
_SEPARATED = re.compile(r"(?<!\d)(\d{1,2})[._](\d{1,2})[._](\d{4}|\d{2})(?!\d)")
_EARLIEST = date(2015, 1, 1)
NOTE = "inferred from folder names and capture dates (the owner's rules, 2026-10-20)"


class PlanChanged(Exception):
    """The tenant's missions are not what the reviewed plan was made from."""


def _day(year: int, month: int, day: int) -> date | None:
    try:
        found = date(year if year > 99 else 2000 + year, month, day)
    except ValueError:
        return None
    return found if _EARLIEST <= found <= date.today() else None


class _Unreadable(Exception):
    pass


def _component_date(component: str) -> date | None:
    if _DATASET.match(component):
        return dataset_date(component)
    if _FIVE.search(component):
        raise _Unreadable(component)
    for m in _SIX.finditer(component):
        found = _day(int(m[3]), int(m[1]), int(m[2]))
        if found is None:
            raise _Unreadable(component)
        return found
    for m in _SEPARATED.finditer(component):
        found = _day(int(m[3]), int(m[1]), int(m[2]))
        if found is None:
            raise _Unreadable(component)
        return found
    return None


def folder_date(source_path: str) -> date | None:
    """The date in the nearest folder that has one; None when none is readable."""
    for component in reversed(source_path.split("/")):
        try:
            found = _component_date(component)
        except _Unreadable:
            return None
        if found is not None:
            return found
    return None


def place_of(source_path: str) -> str | None:
    """The place the nearest folder naming one says, or None."""
    for component in reversed(source_path.split("/")):
        for name, rx in _PLACES:
            if rx.search(component):
                return name
    return None


@dataclass(frozen=True)
class Flag:
    """Something the review should see about one mission."""

    mission: int
    note: str


@dataclass(frozen=True)
class PlannedTrip:
    name: str
    place: str | None
    started_on: date
    ended_on: date
    #: Already a deployment: missions join it, and it is not changed.
    exists: bool
    #: Holds missions of two deployments: nothing is written.
    conflict: bool
    missions: tuple[int, ...]
    #: The missions this trip would put in its deployment.
    to_write: tuple[int, ...]
    sessions: int
    flags: tuple[Flag, ...] = field(default=())


@dataclass(frozen=True)
class InferencePlan:
    trips: tuple[PlannedTrip, ...]
    fingerprint: str


@dataclass(frozen=True)
class Applied:
    missions: int
    deployments: int


@dataclass
class _Mission:
    number: int
    source_path: str
    device_id: uuid.UUID | None
    deployment: str | None
    session: tuple
    captured_on: date
    folder_on: date | None
    place: str | None
    on: date = date.min
    notes: list[str] = field(default_factory=list)


async def _missions(conn, tenant_id, lock: bool) -> list[_Mission]:
    rows = (
        await conn.execute(
            text("""
                SELECT m.number, m.source_path, m.device_id, m.started_at,
                       d.name AS deployment, c.n, c.lo, c.hi
                FROM missions m
                LEFT JOIN deployments d
                  ON d.tenant_id = m.tenant_id AND d.id = m.deployment_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS n, min(captured_at) AS lo,
                           max(captured_at) AS hi
                    FROM captures c
                    WHERE c.tenant_id = m.tenant_id AND c.mission_id = m.id
                ) c ON true
                WHERE m.tenant_id = :t ORDER BY m.number
                """ + (" FOR UPDATE OF m" if lock else "")),
            {"t": tenant_id},
        )
    ).all()
    out = []
    for r in rows:
        last = r.hi or r.started_at
        out.append(
            _Mission(
                number=r.number,
                source_path=r.source_path,
                device_id=r.device_id,
                deployment=r.deployment,
                # Without captures a mission is a session of its own.
                session=(r.device_id, r.lo, r.hi, r.n) if r.n else (r.number,),
                # The camera's local time is stored as UTC (see above).
                captured_on=last.date(),
                folder_on=folder_date(r.source_path),
                place=place_of(r.source_path),
            )
        )
    return out


def _share_between_copies(missions: list[_Mission]) -> None:
    sessions: dict[tuple, list[_Mission]] = defaultdict(list)
    for m in missions:
        sessions[m.session].append(m)
    for copies in sessions.values():
        places = {c.place for c in copies} - {None}
        dates = {c.folder_on for c in copies} - {None}
        for c in copies:
            if c.place is None and len(places) == 1:
                c.place = next(iter(places))
            if c.folder_on is None and len(dates) == 1:
                c.folder_on = next(iter(dates))
    for m in missions:
        m.on = m.folder_on or m.captured_on
        if m.folder_on is None:
            m.notes.append(f"no readable folder date: its captures', {m.on}")
        elif abs((m.folder_on - m.captured_on).days) > 1:
            m.notes.append(
                f"folder says {m.folder_on}, the camera's clock {m.captured_on}"
            )


def _join_by_camera(missions: list[_Mission], gap: int) -> None:
    placed = [m for m in missions if m.place is not None and m.device_id]
    for m in missions:
        if m.place is not None or m.device_id is None:
            continue
        near = sorted(
            (abs((o.on - m.on).days), o.place)
            for o in placed
            if o.device_id == m.device_id and abs((o.on - m.on).days) <= gap
        )
        if near and len({p for d, p in near if d == near[0][0]}) == 1:
            m.place = near[0][1]
            m.notes.append(f"no place in its folders: its camera was at {m.place}")


def _put_together(missions, together: Mapping[int, int]) -> None:
    by_number = {m.number: m for m in missions}
    missing = (set(together) | set(together.values())) - set(by_number)
    if missing:
        raise ValueError(f"no such mission: {', '.join(map(str, sorted(missing)))}")
    for number, other in together.items():
        m, o = by_number[number], by_number[other]
        for c in (c for c in missions if c.session == m.session):
            c.on, c.place = o.on, o.place
            c.notes.append(f"put with mission {other} by the operator")


def _trips(missions: list[_Mission], gap: int) -> list[list[_Mission]]:
    by_place: dict[str | None, list[_Mission]] = defaultdict(list)
    for m in missions:
        by_place[m.place].append(m)
    trips = []
    for group in by_place.values():
        group.sort(key=lambda m: (m.on, m.number))
        current: list[_Mission] = []
        for m in group:
            if current and m.on - current[-1].on > timedelta(days=gap):
                trips.append(current)
                current = []
            current.append(m)
        trips.append(current)
    trips.sort(key=lambda t: (t[0].on, t[0].place or ""))
    return trips


def _name(started_on: date, place: str | None) -> str:
    return f"{started_on:%Y.%m.%d}.FishSense" + (f".{place}" if place else "")


def _fingerprint(gap, trips) -> str:
    # An operator's choice is in it as the flag it leaves on its mission.
    body = {"gap_days": gap, "trips": [asdict(t) for t in trips]}
    encoded = json.dumps(body, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


async def plan_inference(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    gap_days: int = 2,
    together: Mapping[int, int] | None = None,
    _lock: bool = False,
) -> InferencePlan:
    """The deployments the tenant's missions would be grouped into. Writes nothing."""
    together = dict(together or {})
    missions = await _missions(conn, tenant_id, _lock)
    _share_between_copies(missions)
    _put_together(missions, together)
    _join_by_camera(missions, gap_days)
    existing = {
        r.name
        for r in await conn.execute(
            text("SELECT name FROM deployments WHERE tenant_id = :t"), {"t": tenant_id}
        )
    }

    planned = []
    for trip in _trips(missions, gap_days):
        place = trip[0].place
        started_on, ended_on = trip[0].on, trip[-1].on
        held = sorted({m.deployment for m in trip} - {None})
        flags = [Flag(m.number, n) for m in trip for n in m.notes]
        name = held[0] if len(held) == 1 else _name(started_on, place)
        conflict = len(held) > 1
        if conflict:
            flags.append(Flag(0, f"spans deployments {', '.join(held)}: left alone"))
        elif not held and name in existing:
            conflict = True
            flags.append(Flag(0, f"{name} already names another trip: left alone"))
        planned.append(
            PlannedTrip(
                name=name,
                place=place,
                started_on=started_on,
                ended_on=ended_on,
                exists=len(held) == 1,
                conflict=conflict,
                missions=tuple(sorted(m.number for m in trip)),
                to_write=(
                    ()
                    if conflict
                    else tuple(sorted(m.number for m in trip if m.deployment is None))
                ),
                sessions=len({m.session for m in trip}),
                flags=tuple(sorted(flags, key=lambda f: (f.mission, f.note))),
            )
        )
    trips = tuple(planned)
    return InferencePlan(trips=trips, fingerprint=_fingerprint(gap_days, trips))


async def apply_inference(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    expect: str,
    gap_days: int = 2,
    together: Mapping[int, int] | None = None,
) -> Applied:
    """Write the reviewed plan, all or nothing, inside the caller's transaction."""
    plan = await plan_inference(
        conn, tenant_id, gap_days=gap_days, together=together, _lock=True
    )
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the plan is now {plan.fingerprint}, not the reviewed {expect}: "
            "review it again"
        )
    written = created = 0
    for trip in plan.trips:
        if not trip.to_write:
            continue
        if trip.exists:
            deployment_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM deployments WHERE tenant_id = :t AND name = :n"
                    ),
                    {"t": tenant_id, "n": trip.name},
                )
            ).scalar_one()
        else:
            deployment_id = (
                await conn.execute(
                    text("""
                        INSERT INTO deployments (tenant_id, name, started_on,
                                                 ended_on, notes)
                        VALUES (:t, :n, :s, :e, :notes) RETURNING id
                        """),
                    {
                        "t": tenant_id,
                        "n": trip.name,
                        "s": trip.started_on,
                        "e": trip.ended_on,
                        "notes": f"{NOTE}: {trip.sessions} sessions, "
                        f"{len(trip.missions)} missions",
                    },
                )
            ).scalar_one()
            created += 1
        for number in trip.to_write:
            result = await conn.execute(
                text(
                    "UPDATE missions SET deployment_id = :d WHERE tenant_id = :t "
                    "AND number = :n AND deployment_id IS NULL"
                ),
                {"t": tenant_id, "n": number, "d": deployment_id},
            )
            if result.rowcount != 1:
                raise PlanChanged(f"mission {number} changed while being written")
            written += 1
    return Applied(missions=written, deployments=created)
