"""The database side of the web portal, tenant-scoped.

Ported from fishsense-lite@77e8f8e5, services/fishsense-api:

* `label_controller.py` `get_{laser,headtail,species,dive_slate}_label_studio_project_ids`
  and `_gate_scan`: which Label Studio projects still hold live labeling work.
  The landing page and triage both ask this ("one definition, two consumers",
  apps/fishsense-lite-web/lib/label-projects.ts);
* `dive_controller.py` `get_dives`, `set_dive_calibration_source`,
  `clear_dive_calibration_source`: the calibration-linking page.

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
  visible, so it can't be borrowed from (PLAN.md §9.17).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "DiveNotFound",
    "DiveSummary",
    "GateNotApplicable",
    "LabelKind",
    "SelfLink",
    "clear_calibration_source",
    "label_studio_project_ids",
    "list_dives",
    "set_calibration_source",
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


class DiveNotFound(LookupError):
    """No such dive in this tenant; ``which`` says which one was missing."""

    def __init__(self, which: str, number: int) -> None:
        super().__init__(f"{which} {number} not found")
        self.which = which
        self.number = number


@dataclass(frozen=True)
class DiveSummary:
    """What the portal shows of a dive. v1's `id`, `dive_datetime`,
    `dive_slate_id` and `calibration_dive_id` are `number`, `dived_at`, the
    slate template's `number` and the source dive's `number`."""

    number: int
    name: str | None
    dived_at: datetime
    priority: str
    slate_template_number: int | None
    calibration_source_number: int | None


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


_DIVE_SUMMARY = """
    SELECT d.number, d.name, d.dived_at, d.priority,
           t.number AS slate_template_number,
           s.number AS calibration_source_number
    FROM dives d
    LEFT JOIN slate_templates t ON t.id = d.slate_template_id
    LEFT JOIN dives s
      ON s.tenant_id = d.tenant_id AND s.id = d.calibration_source_dive_id
    WHERE d.tenant_id = :tenant_id
"""


async def list_dives(conn: AsyncConnection, tenant_id: uuid.UUID) -> list[DiveSummary]:
    """Every dive in the tenant, by number (v1: every dive, unordered)."""
    rows = await conn.execute(
        text(_DIVE_SUMMARY + " ORDER BY d.number"), {"tenant_id": tenant_id}
    )
    return [DiveSummary(**row._mapping) for row in rows]


async def _dive_id(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, which: str
) -> uuid.UUID:
    found = (
        await conn.execute(
            text(
                "SELECT id FROM dives WHERE tenant_id = :tenant_id AND number = :n "
                "FOR UPDATE"
            ),
            {"tenant_id": tenant_id, "n": number},
        )
    ).scalar_one_or_none()
    if found is None:
        raise DiveNotFound(which, number)
    return found


async def _summary(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> DiveSummary:
    row = (
        await conn.execute(
            text(_DIVE_SUMMARY + " AND d.number = :n"),
            {"tenant_id": tenant_id, "n": number},
        )
    ).one()
    return DiveSummary(**row._mapping)


async def set_calibration_source(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, source_number: int
) -> DiveSummary:
    """Link dive `number` to borrow dive `source_number`'s laser calibration.

    For a fish-only dive with no slate of its own: point it at a sibling slate
    dive shot with the same camera and laser rig. The effective calibration
    then falls back to the source when the dive has none of its own.

    Like v1, the source need not have a slate; only the portal restricts that.
    """
    if number == source_number:
        raise SelfLink("A dive cannot be its own calibration source")
    dive = await _dive_id(conn, tenant_id, number, "dive")
    source = await _dive_id(conn, tenant_id, source_number, "calibration source dive")
    await conn.execute(
        text(
            "UPDATE dives SET calibration_source_dive_id = :source "
            "WHERE tenant_id = :tenant_id AND id = :dive"
        ),
        {"tenant_id": tenant_id, "dive": dive, "source": source},
    )
    return await _summary(conn, tenant_id, number)


async def clear_calibration_source(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> None:
    """Unlink dive `number` from any borrowed calibration (idempotent)."""
    dive = await _dive_id(conn, tenant_id, number, "dive")
    await conn.execute(
        text(
            "UPDATE dives SET calibration_source_dive_id = NULL "
            "WHERE tenant_id = :tenant_id AND id = :dive"
        ),
        {"tenant_id": tenant_id, "dive": dive},
    )
