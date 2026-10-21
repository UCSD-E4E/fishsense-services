"""The database side of the web portal, tenant-scoped.

Ported from fishsense-lite@77e8f8e5, services/fishsense-api:

* `label_controller.py` `get_{laser,headtail,species,dive_slate}_label_studio_project_ids`
  and `_gate_scan`: which Label Studio projects still hold live labeling work.
  The landing page and triage both ask this ("one definition, two consumers",
  apps/fishsense-lite-web/lib/label-projects.ts);
* `mission_controller.py` `get_missions`, `set_mission_calibration_source`,
  `clear_mission_calibration_source`: the calibration-linking page; and
  `set_mission_calibration_target`, `clear_mission_calibration_target`,
  `clear_calibration_refused`: the levers a calibration refusal's remedy
  names.

v2 changes:

* one query serves the four label kinds (v1 had four endpoints), per tenant;
* the auto-accept gate reads each capture's **current** prediction and its
  effective verdict (`current_laser_predictions_gated`): v2's gate appends
  verdicts to their own table, and v1's "a re-prediction clears the verdict"
  is a newer prediction with none;
* `gated` for a kind with no gate is refused (:class:`GateNotApplicable`),
  where v1 simply had no such parameter;
* dives are addressed by `number` (v1's id for a migrated dive) and the
  calibration source is another dive's `number`; another tenant's dive is not
  visible, so it can't be borrowed from (PLAN.md §9.17). A calibration
  target is its row's `number` (v1's id for a migrated one);
* a refusal is an append-only row, so clearing one appends a clear
  (`laser_calibration_store.clear_calibration_refusal`), and setting a target
  stamps the link change that expires a refusal (`species_store`), where v1
  nulled the dive's refusal columns.
"""

import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Literal, get_args

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.camera_sql import ADMITTED_PORT, RECTIFIABLE_MISSION

from fishsense_services_api import (
    headtail_store,
    laser_calibration_store,
    laser_store,
    slate_store,
    species_store,
)

__all__ = [
    "CalibrationTargetNotFound",
    "DiveNotFound",
    "MissionDetail",
    "MissionSummary",
    "GateNotApplicable",
    "LabelKind",
    "SelfLink",
    "clear_calibration_refusal",
    "clear_calibration_source",
    "clear_calibration_target",
    "label_studio_project_ids",
    "list_missions",
    "mission_detail",
    "mission_summary",
    "set_calibration_source",
    "set_calibration_target",
    "set_needs_reprocess",
]

#: The label kinds, spelled as `label_studio_projects.kind` spells them.
LabelKind = Literal["laser", "head_tail", "species", "slate"]

_LABEL_TABLES: dict[str, str] = {
    "laser": "laser_labels",
    "head_tail": "head_tail_labels",
    "species": "species_labels",
    "slate": "slate_labels",
}
assert set(_LABEL_TABLES) == set(get_args(LabelKind))

#: Kinds whose predictions carry an auto-accept gate verdict. Only laser does
#: (`laser_predictions.gate_verdict`), as in v1.
GATED_KINDS = frozenset({"laser"})


class GateNotApplicable(ValueError):
    """`gated` was asked of a kind that has no auto-accept gate."""


class SelfLink(ValueError):
    """A dive cannot be its own calibration source."""


class CalibrationTargetNotFound(LookupError):
    """No calibration target has this number."""

    def __init__(self, number: int) -> None:
        super().__init__(f"calibration target {number} not found")
        self.number = number


class DiveNotFound(LookupError):
    """No such dive in this tenant; ``which`` says which one was missing."""

    def __init__(self, which: str, number: int) -> None:
        super().__init__(f"{which} {number} not found")
        self.which = which
        self.number = number


@dataclass(frozen=True)
class MissionSummary:
    """What the portal shows of a dive. v1's `id`, `dive_datetime`,
    `dive_slate_id` and `calibration_dive_id` are `number`, `started_at`, the
    slate template's `number` and the source dive's `number`."""

    number: int
    name: str | None
    #: ``lite``, a folder off a camera, or ``mobile``, a phone's outing (0057).
    source: str
    started_at: datetime
    priority: str
    slate_template_number: int | None
    calibration_target_number: int | None
    calibration_source_number: int | None
    #: What the mission is for, its camera port and what said so, and its
    #: field trip (0047).
    purpose: str | None
    port: str
    port_source: str | None
    site: str | None
    deployment_number: int | None
    deployment_name: str | None
    #: The stages will take it: the corrective optic and a pinhole camera,
    #: and any borrowed calibration's source behind the corrective optic too
    #: (`camera_sql`, 2b-2). Computed here so no client restates the rule.
    processable: bool
    #: Its camera, and how many frames it holds: Data's pages show both for
    #: every mission (docs/design/README.md).
    device_name: str | None
    captures: int


def _gate_scan(*, judged: bool) -> str:
    """Correlated EXISTS body (v1's `_gate_scan`): does the outer label's
    project hold a live-labelled capture whose current laser prediction has
    (or has not) been judged by the gate?

    The inner label table is aliased so it can't be mistaken for the outer one,
    and `superseded` is applied inside the scan: a dead-lettered label is not
    live work, so a pending prediction on its capture must not keep an
    otherwise-finished project off the landing page.
    """
    verdict = "IS NOT NULL" if judged else "IS NULL"
    return f"""
        EXISTS (
            SELECT 1 FROM laser_labels g
            -- The effective verdict: v2's gate appends to laser_prediction_verdicts
            -- (migration 0021); only a migrated v1 row carries its own.
            JOIN current_laser_predictions_gated p
              ON p.tenant_id = g.tenant_id AND p.capture_id = g.capture_id
            WHERE g.tenant_id = l.tenant_id
              AND g.ls_project_id = l.ls_project_id
              AND NOT g.superseded
              AND p.gate_verdict {verdict}
        )
    """


async def label_studio_project_ids(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    kind: LabelKind,
    *,
    incomplete: bool = False,
    gated: bool | None = None,
) -> list[int]:
    """Distinct Label Studio project ids with at least one live label of `kind`.

    * Live: not superseded, and in a project (a sentinel is in none).
    * `incomplete`: only projects with a label not yet completed.
    * `gated=True` (laser only): the auto-accept gate has **finished** with the
      project -- no live frame's prediction awaits a verdict, and at least one
      has one. "Done here", not "has run here": a half-swept dive still holds
      frames the gate is about to take. `gated=False` is the exact complement;
      `None` keeps every project.

    Ordered by project id, so the answer is stable (v1's was unordered).
    """
    if gated is not None and kind not in GATED_KINDS:
        raise GateNotApplicable(f"{kind} has no auto-accept gate")
    table = _LABEL_TABLES[kind]
    conditions = [
        "l.tenant_id = :tenant_id",
        "l.ls_project_id IS NOT NULL",
        "NOT l.superseded",
    ]
    if incomplete:
        conditions.append("NOT l.completed")
    if gated is not None:
        finished = f"({_gate_scan(judged=True)} AND NOT {_gate_scan(judged=False)})"
        conditions.append(finished if gated else f"NOT {finished}")
    rows = await conn.execute(
        text(
            f"SELECT DISTINCT l.ls_project_id FROM {table} l "
            f"WHERE {' AND '.join(conditions)} ORDER BY l.ls_project_id"
        ),
        {"tenant_id": tenant_id},
    )
    return list(rows.scalars())


_MISSION_COLUMNS = f"""
    d.number, d.name, d.source, d.started_at, d.priority,
    t.number AS slate_template_number,
    g.number AS calibration_target_number,
    s.number AS calibration_source_number,
    d.purpose, d.port, d.port_source, d.site,
    p.number AS deployment_number, p.name AS deployment_name,
    ({RECTIFIABLE_MISSION} AND {ADMITTED_PORT}) AS processable,
    v.name AS device_name,
    (SELECT count(*) FROM captures c
     WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id) AS captures
"""
_MISSION_JOINS = """
    LEFT JOIN slate_templates t ON t.id = d.slate_template_id
    LEFT JOIN calibration_targets g ON g.id = d.calibration_target_id
    LEFT JOIN missions s
      ON s.tenant_id = d.tenant_id AND s.id = d.calibration_source_mission_id
    LEFT JOIN deployments p
      ON p.tenant_id = d.tenant_id AND p.id = d.deployment_id
    LEFT JOIN devices v ON v.tenant_id = d.tenant_id AND v.id = d.device_id
"""
_MISSION_SUMMARY = f"""
    SELECT {_MISSION_COLUMNS} FROM missions d {_MISSION_JOINS}
    WHERE d.tenant_id = :tenant_id
"""
#: One mission's page: the summary and the rest of the row, in one query.
_MISSION_DETAIL = f"""
    SELECT {_MISSION_COLUMNS},
           d.source_path, d.notes, d.port_note, d.country, d.region
    FROM missions d {_MISSION_JOINS}
    WHERE d.tenant_id = :tenant_id AND d.number = :n
"""


async def list_missions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    deployment_number: int | None = None,
) -> list[MissionSummary]:
    """Every dive in the tenant, by number (v1: every dive, unordered); only
    those of one deployment when ``deployment_number`` is given."""
    where = "" if deployment_number is None else " AND p.number = :deployment"
    rows = await conn.execute(
        text(_MISSION_SUMMARY + where + " ORDER BY d.number"),
        {"tenant_id": tenant_id, "deployment": deployment_number},
    )
    return [MissionSummary(**row._mapping) for row in rows]


async def mission_summary(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> MissionSummary:
    """Mission ``number`` as the portal shows it; `DiveNotFound` if missing."""
    row = (
        await conn.execute(
            text(_MISSION_SUMMARY + " AND d.number = :n"),
            {"tenant_id": tenant_id, "n": number},
        )
    ).one_or_none()
    if row is None:
        raise DiveNotFound("mission", number)
    return MissionSummary(**row._mapping)


@dataclass(frozen=True)
class MissionDetail:
    """A mission's page: its summary and the rest of the row (0047)."""

    summary: MissionSummary
    #: A phone mission has no folder (0057).
    source_path: str | None
    notes: str | None
    port_note: str | None
    country: str | None
    region: str | None


async def mission_detail(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> MissionDetail:
    """Mission ``number``'s page, in one query; `DiveNotFound` if missing."""
    row = (
        await conn.execute(text(_MISSION_DETAIL), {"tenant_id": tenant_id, "n": number})
    ).one_or_none()
    if row is None:
        raise DiveNotFound("mission", number)
    values = dict(row._mapping)
    summary = MissionSummary(
        **{f.name: values.pop(f.name) for f in fields(MissionSummary)}
    )
    return MissionDetail(summary=summary, **values)


async def _mission_id(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, which: str
) -> uuid.UUID:
    found = (
        await conn.execute(
            text(
                "SELECT id FROM missions WHERE tenant_id = :tenant_id AND number = :n "
                "FOR UPDATE"
            ),
            {"tenant_id": tenant_id, "n": number},
        )
    ).scalar_one_or_none()
    if found is None:
        raise DiveNotFound(which, number)
    return found


async def set_calibration_source(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, source_number: int
) -> MissionSummary:
    """Link dive `number` to borrow dive `source_number`'s laser calibration.

    For a fish-only dive with no slate of its own: point it at a sibling slate
    dive shot with the same camera and laser rig. The effective calibration
    then falls back to the source when the dive has none of its own.

    Like v1, the source need not have a slate; only the portal restricts that.
    """
    if number == source_number:
        raise SelfLink("A mission cannot be its own calibration source")
    mission = await _mission_id(conn, tenant_id, number, "mission")
    source = await _mission_id(
        conn, tenant_id, source_number, "calibration source mission"
    )
    await conn.execute(
        text(
            "UPDATE missions SET calibration_source_mission_id = :source "
            "WHERE tenant_id = :tenant_id AND id = :mission"
        ),
        {"tenant_id": tenant_id, "mission": mission, "source": source},
    )
    return await mission_summary(conn, tenant_id, number)


async def clear_calibration_source(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> None:
    """Unlink dive `number` from any borrowed calibration (idempotent)."""
    mission = await _mission_id(conn, tenant_id, number, "mission")
    await conn.execute(
        text(
            "UPDATE missions SET calibration_source_mission_id = NULL "
            "WHERE tenant_id = :tenant_id AND id = :mission"
        ),
        {"tenant_id": tenant_id, "mission": mission},
    )


async def set_calibration_target(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, target_number: int
) -> MissionSummary:
    """Declare which board dive `number` was shot against: the calibration
    target numbered `target_number` (v1's id for a migrated one).

    v1's `set_dive_calibration_target`, an operator's counterpart of the
    species sync's write -- through the same setter, so it expires a standing
    refusal as v1's `_clear_refusal` did. The board's geometry is read from
    its current version by name, whichever version is named here.
    """
    mission = await _mission_id(conn, tenant_id, number, "mission")
    target = (
        await conn.execute(
            text("SELECT id FROM calibration_targets WHERE number = :n"),
            {"n": target_number},
        )
    ).scalar_one_or_none()
    if target is None:
        raise CalibrationTargetNotFound(target_number)
    await species_store.set_mission_calibration_target(conn, tenant_id, mission, target)
    return await mission_summary(conn, tenant_id, number)


async def clear_calibration_target(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> None:
    """Unlink dive `number` from any board (idempotent): what a refusal's
    remedy asks of a checkerboard dive that needs other frames."""
    mission = await _mission_id(conn, tenant_id, number, "mission")
    await species_store.clear_mission_calibration_target(conn, tenant_id, mission)


async def clear_calibration_refusal(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    number: int,
    *,
    reason: str | None = None,
) -> bool:
    """Clear dive `number`'s standing calibration refusal, so the dive is
    offered again (v1's `DELETE /dives/{id}/calibration-refused/`). An
    appended clear; idempotent, False when nothing stood."""
    mission = await _mission_id(conn, tenant_id, number, "mission")
    return await laser_calibration_store.clear_calibration_refusal(
        conn, tenant_id, mission, reason=reason
    )


async def set_needs_reprocess(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    number: int,
    kind: LabelKind,
    *,
    raised: bool,
    only_incomplete: bool = True,
) -> int:
    """Ask for dive `number`'s `kind` frames to be redrawn, or withdraw that;
    the rows touched (v1's `PUT`/`DELETE .../labels/{kind}/needs-reprocess`).

    Each kind's store owns its rule -- which rows a raise may touch, and that
    a withdrawal touches every row -- so this only resolves the dive and
    dispatches. A withdrawal is the whole dive: an operator has no redraw run
    to scope it to.
    """
    mission = await _mission_id(conn, tenant_id, number, "mission")
    if kind == "species":
        return await species_store.set_species_needs_reprocess(
            conn, tenant_id, mission, raised, only_incomplete=only_incomplete
        )
    if raised:
        raise_flags = {
            "laser": laser_store.raise_laser_reprocess_flags,
            "head_tail": headtail_store.set_headtail_needs_reprocess,
            "slate": slate_store.flag_slate_labels_for_reprocess,
        }[kind]
        return await raise_flags(
            conn, tenant_id, mission, only_incomplete=only_incomplete
        )
    if kind == "laser":
        return await laser_store.clear_laser_reprocess_flags(
            conn, tenant_id, mission, None
        )
    if kind == "head_tail":
        return await headtail_store.clear_headtail_needs_reprocess(
            conn, tenant_id, mission, None
        )
    return await slate_store.clear_slate_reprocess_flags(
        conn, tenant_id, mission, checksums=None
    )
