"""Recording which slate template each mission used, where it has none (the
owner, 2026-10-26).

The owner decided that template choices for missions with no
``slate_template_id`` go into mission metadata. They come from the owner's
visual labels of the V-Slate variants and from the automatic TTT
identification, as a mapping file of mission number to template (by v1 id or
by name) with a free-text source.

**Only an empty link is filled.** A mission already on the same template is
listed as unchanged; one on another is a conflict, listed and left alone --
an operator who means to change it does so through the portal, deliberately.

**Setting the link does what v2's own setter does.**
``species_store.set_mission_slate_template`` (v1's ``set_dive_slate``, whose
``_clear_refusal`` ran on every write) stamps
``missions.calibration_links_changed_at`` (0022), and a refused calibration
older than the stamp no longer stands (``REFUSAL_OUTLIVED_SQL``, read by both
calibration cohorts, frozen into ``dive_pipeline_status`` by 0029). So does
this. The stamp expires *every* standing refusal of the mission, the
checkerboard's included, so a mission whose board fit was refused is tried
again.

**The plan says what the human pipeline will do with it**, because a
template sets off work the owner should see before applying: which missions
then enter the stage-9 slate-preprocessing cohort (``slate_store``: marked or
detected slate frames with no live slate label -- new Label Studio tasks, and
how many frames), the stage-13 laser-calibration cohort, and the checkerboard
cohort (entering, through the stamp; leaving, when stage 13 can now
calibrate instead). Each is the store's own predicate, evaluated on the
mission as the apply would leave it (``jsonb_populate_record`` over the
row, with the template and the stamp swapped in), with the selectors'
``priority = 'high'`` term: what the orchestrator would actually pick up.
Read-only.

A plan's fingerprint covers what it would write and those side effects. The
apply re-plans under a lock and writes only the plan the operator reviewed,
all or nothing, guarded on the link still being empty (as
``mission_backfill``).

Runs in a tenant transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.laser_calibration_store import (
    CHECKERBOARD_CALIBRATION_COHORT,
    LASER_CALIBRATION_COHORT,
)
from fishsense_services_api.mission_backfill import PlanChanged, plan_fingerprint
from fishsense_services_api.slate_store import (
    slate_preprocess_cohort,
    slate_preprocess_frame,
)
from fishsense_services_api.taxonomy_sql import SLATE_CONTENT_MARKER

__all__ = [
    "Applied",
    "AssignmentPlan",
    "AssignmentRefused",
    "Conflict",
    "PlannedAssignment",
    "SlateWork",
    "apply_assignment",
    "plan_assignment",
]


class AssignmentRefused(ValueError):
    """The mapping can't be planned as given; the message says why."""


@dataclass(frozen=True)
class PlannedAssignment:
    mission_number: int
    template_id: uuid.UUID
    template_name: str


@dataclass(frozen=True)
class Conflict:
    """The mission is on another template already; it is left alone."""

    mission_number: int
    current_template_id: uuid.UUID
    current_template_name: str
    wanted_template_id: uuid.UUID
    wanted_template_name: str


@dataclass(frozen=True)
class SlateWork:
    """A mission that enters stage 9, and the frames it would queue."""

    mission_number: int
    frames: int


@dataclass(frozen=True)
class AssignmentPlan:
    source: str
    assign: tuple[PlannedAssignment, ...]
    #: Already on the template the mapping names.
    unchanged: tuple[int, ...]
    conflicts: tuple[Conflict, ...]
    #: Missions entering the stage-9 cohort: new Label Studio work.
    slate_preprocessing: tuple[SlateWork, ...]
    #: Missions entering the stage-13 (slate) laser-calibration cohort.
    laser_calibration: tuple[int, ...]
    #: Missions entering the checkerboard cohort (a refusal the stamp outlives).
    checkerboard_entering: tuple[int, ...]
    #: Missions leaving it, now that stage 13 can calibrate them.
    checkerboard_leaving: tuple[int, ...]
    fingerprint: str


@dataclass(frozen=True)
class Applied:
    missions: int


def _parse(mapping: Mapping) -> tuple[str, dict[int, tuple[str, int | str]]]:
    """The mapping file's source, and each mission's template reference."""
    if not isinstance(mapping, Mapping):
        raise AssignmentRefused("the mapping is a JSON object")
    source = mapping.get("source")
    if not isinstance(source, str) or not source.strip():
        raise AssignmentRefused('say where the choices come from: a non-empty "source"')
    missions = mapping.get("missions")
    if not isinstance(missions, Mapping) or not missions:
        raise AssignmentRefused("no missions in the mapping")
    wanted: dict[int, tuple[str, int | str]] = {}
    for key, entry in missions.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            raise AssignmentRefused(f"not a mission number: {key!r}") from None
        keys = set(entry) if isinstance(entry, Mapping) else set()
        if keys == {"template_v1"} and type(entry["template_v1"]) is int:
            wanted[number] = ("v1", entry["template_v1"])
        elif keys == {"template"} and isinstance(entry["template"], str):
            wanted[number] = ("name", entry["template"])
        else:
            raise AssignmentRefused(
                f"mission {number}: give exactly one of template_v1 (an integer) "
                "or template (a name)"
            )
    return source.strip(), wanted


async def _templates(conn, wanted) -> dict[int, tuple[uuid.UUID, str]]:
    rows = (
        await conn.execute(text("SELECT id, name, v1_id FROM slate_templates"))
    ).all()
    by_v1 = {r.v1_id: r for r in rows if r.v1_id is not None}
    by_name = {r.name: r for r in rows}
    resolved = {}
    for number, (kind, ref) in sorted(wanted.items()):
        row = (by_v1 if kind == "v1" else by_name).get(ref)
        if row is None:
            raise AssignmentRefused(
                f"mission {number}: no slate template "
                + (f"with v1 id {ref}" if kind == "v1" else f"named {ref!r}")
            )
        resolved[number] = (row.id, row.name)
    return resolved


#: The selectors' terms over dive `d`, as `next_mission_for_*` add them.
_STAGE_9 = (
    f"d.priority = 'high' AND {slate_preprocess_cohort(':marker', detector=True)}"
)
_STAGE_13 = f"d.priority = 'high' AND {LASER_CALIBRATION_COHORT}"
_BOARD = f"d.priority = 'high' AND {CHECKERBOARD_CALIBRATION_COHORT}"


async def _cohorts(conn, tenant_id, assign: list[PlannedAssignment], *, after: bool):
    """Each mission's membership of the three cohorts, as it is now or (with
    `after`) as the apply would leave it: the template set and the link
    change stamped at this transaction's `now()`, as the apply stamps it."""
    if after:
        mission = """jsonb_populate_record(m, jsonb_build_object(
            'slate_template_id', w.template,
            'calibration_links_changed_at', now())) d"""
    else:
        mission = "(SELECT m.*) d"
    rows = await conn.execute(
        text(f"""
            SELECT d.number,
                   {_STAGE_9} AS stage_9,
                   (SELECT count(*) FROM captures c
                    WHERE {slate_preprocess_frame(":marker", detector=True)}
                   ) AS frames,
                   {_STAGE_13} AS stage_13,
                   {_BOARD} AS board
            FROM missions m
            JOIN unnest(CAST(:numbers AS integer[]), CAST(:templates AS uuid[]))
                 AS w(number, template) ON w.number = m.number
            CROSS JOIN LATERAL {mission}
            WHERE m.tenant_id = :t
            """),
        {
            "t": tenant_id,
            "marker": SLATE_CONTENT_MARKER,
            "numbers": [a.mission_number for a in assign],
            "templates": [a.template_id for a in assign],
        },
    )
    return {r.number: r for r in rows}


async def plan_assignment(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mapping: Mapping,
    *,
    _lock: bool = False,
) -> AssignmentPlan:
    """What recording these templates would write, and set off. Read-only."""
    source, wanted = _parse(mapping)
    templates = await _templates(conn, wanted)
    rows = {
        r.number: r
        for r in await conn.execute(
            text(f"""
                SELECT m.number, m.slate_template_id, t.name AS template_name
                FROM missions m LEFT JOIN slate_templates t
                  ON t.id = m.slate_template_id
                WHERE m.tenant_id = :t AND m.number = ANY(CAST(:n AS integer[]))
                {"FOR UPDATE OF m" if _lock else ""}
                """),
            {"t": tenant_id, "n": sorted(wanted)},
        )
    }
    missing = sorted(set(wanted) - set(rows))
    if missing:
        raise AssignmentRefused(f"no mission {', '.join(map(str, missing))}")
    assign: list[PlannedAssignment] = []
    unchanged: list[int] = []
    conflicts: list[Conflict] = []
    for number in sorted(wanted):
        template_id, name = templates[number]
        row = rows[number]
        if row.slate_template_id is None:
            assign.append(PlannedAssignment(number, template_id, name))
        elif row.slate_template_id == template_id:
            unchanged.append(number)
        else:
            conflicts.append(
                Conflict(number, row.slate_template_id, row.template_name,
                         template_id, name)
            )  # fmt: skip
    before = await _cohorts(conn, tenant_id, assign, after=False)
    after = await _cohorts(conn, tenant_id, assign, after=True)

    def entering(cohort: str) -> tuple[int, ...]:
        return tuple(
            n
            for n in sorted(after)
            if getattr(after[n], cohort) and not getattr(before[n], cohort)
        )

    slate_work = tuple(SlateWork(n, after[n].frames) for n in entering("stage_9"))
    leaving = tuple(n for n in sorted(after) if before[n].board and not after[n].board)
    body = {
        "source": source,
        "assign": [asdict(a) for a in assign],
        "unchanged": unchanged,
        "conflicts": [asdict(c) for c in conflicts],
        "slate_preprocessing": [asdict(w) for w in slate_work],
        "laser_calibration": list(entering("stage_13")),
        "checkerboard_entering": list(entering("board")),
        "checkerboard_leaving": list(leaving),
    }
    return AssignmentPlan(
        source=source,
        assign=tuple(assign),
        unchanged=tuple(unchanged),
        conflicts=tuple(conflicts),
        slate_preprocessing=slate_work,
        laser_calibration=entering("stage_13"),
        checkerboard_entering=entering("board"),
        checkerboard_leaving=leaving,
        fingerprint=plan_fingerprint(body),
    )


async def apply_assignment(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mapping: Mapping,
    *,
    expect: str,
) -> Applied:
    """Write the reviewed plan, all or nothing, inside the caller's transaction."""
    plan = await plan_assignment(conn, tenant_id, mapping, _lock=True)
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the plan is now {plan.fingerprint}, not the reviewed {expect}: "
            "review it again"
        )
    if not plan.assign:
        return Applied(missions=0)
    written = (
        await conn.execute(
            text("""
                UPDATE missions m SET slate_template_id = w.template,
                                      calibration_links_changed_at = now()
                FROM unnest(CAST(:numbers AS integer[]), CAST(:templates AS uuid[]))
                     AS w(number, template)
                WHERE m.tenant_id = :t AND m.number = w.number
                  AND m.slate_template_id IS NULL
                """),
            {
                "t": tenant_id,
                "numbers": [a.mission_number for a in plan.assign],
                "templates": [a.template_id for a in plan.assign],
            },
        )
    ).rowcount
    if written != len(plan.assign):
        raise PlanChanged(
            f"set {written} missions' templates, not the plan's {len(plan.assign)}"
        )
    return Applied(missions=written)
