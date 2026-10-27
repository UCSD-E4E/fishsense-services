"""Retiring a mission's slate Label Studio project on purpose (0079; the
owner, 2026-10-26).

``assign-slate-templates`` gave mission 508, a pool test that must never have
a dive slate, a slate template from an automatic vote, and stage 9 (the
``preprocess-slate-images`` schedule) made it a slate project, 300745, of 47
placeholder labels: no reference points, not completed. Mission 439 got one
too (300747, 9 placeholders), which the owner does not want labeled now.
Nothing could clear a template, and nothing could take a project back: the
sync lists every project holding a live label, so deleting one in Label
Studio would fail every sync run.

What a retirement does, and does not:

* the mission's live slate projects are retired (``retired_at``,
  ``retired_reason``): not synced, not found again as the mission's project,
  not recorded again (``label_sync_store``, ``slate_store``,
  ``label_project_store``);
* their placeholders (not completed, not superseded) are superseded with the
  reason. A label retired with a reason is settled (``slate_store._settled``):
  stage 9 does not draw its frame again;
* a completed label is a person's work. The plan is refused while one is
  live in those projects, unless the operator allows it, and even then it is
  not superseded: it stays live (and counts for calibration), only no longer
  synced. The plan reports them;
* with ``clear_template``, the mission's slate template is cleared, matched on
  the exact template the plan names, and ``calibration_links_changed_at``
  stamped as ``slate_template_assignment`` does (a standing refusal no longer
  stands);
* no row is deleted and no point moved.

A project holding another mission's live labels (a v1 shared project) is
refused: retiring it would stop the sync for that mission too.

A plan is read-only; its fingerprint covers everything it would write. The
apply re-plans under a lock and writes only the plan the operator reviewed,
all or nothing: each UPDATE checks its row count (as
``slate_label_retirement``).

Runs in a tenant transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_backfill import PlanChanged, plan_fingerprint

__all__ = [
    "PlannedProject",
    "ProjectRetirementApplied",
    "ProjectRetirementPlan",
    "ProjectRetirementRefused",
    "apply_project_retirement",
    "plan_project_retirement",
]


class ProjectRetirementRefused(ValueError):
    """The project can't be retired as asked; the message says why."""


@dataclass(frozen=True)
class PlannedProject:
    ls_project_id: int
    title: str | None
    #: Not completed, not superseded: superseded with the reason.
    placeholders: tuple[uuid.UUID, ...]
    #: Completed and live (a person's work): kept live, no longer synced.
    completed: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class ProjectRetirementPlan:
    mission_id: uuid.UUID
    mission_number: int
    reason: str
    projects: tuple[PlannedProject, ...]
    #: The mission's slate template, as the plan found it.
    template_id: uuid.UUID | None
    template_name: str | None
    clear_template: bool
    fingerprint: str


@dataclass(frozen=True)
class ProjectRetirementApplied:
    projects: int
    placeholders: int
    template_cleared: bool


async def plan_project_retirement(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_number: int,
    reason: str,
    *,
    clear_template: bool,
    allow_completed: bool = False,
    _lock: bool = False,
) -> ProjectRetirementPlan:
    """What retiring the mission's slate projects would do. Read-only."""
    reason = (reason or "").strip()
    if not reason:
        raise ProjectRetirementRefused("say why the project is retired")

    def lock(alias: str) -> str:
        return f" FOR UPDATE OF {alias}" if _lock else ""

    found = (
        await conn.execute(
            text(f"""
                SELECT m.id, m.slate_template_id, t.name AS template_name
                FROM missions m
                LEFT JOIN slate_templates t ON t.id = m.slate_template_id
                WHERE m.tenant_id = :t AND m.number = :n{lock("m")}
                """),
            {"t": tenant_id, "n": mission_number},
        )
    ).one_or_none()
    if found is None:
        raise ProjectRetirementRefused(f"no mission {mission_number}")
    if clear_template and found.slate_template_id is None:
        raise ProjectRetirementRefused(
            f"mission {mission_number} has no slate template to clear"
        )
    records = (
        await conn.execute(
            text(f"""
                SELECT p.ls_project_id, p.title FROM label_studio_projects p
                WHERE p.tenant_id = :t AND p.mission_id = :m AND p.kind = 'slate'
                  AND p.retired_at IS NULL
                ORDER BY p.ls_project_id{lock("p")}
                """),
            {"t": tenant_id, "m": found.id},
        )
    ).all()
    if not records:
        raise ProjectRetirementRefused(
            f"mission {mission_number} has no slate project to retire"
        )
    labels = (
        await conn.execute(
            text(f"""
                SELECT s.id, s.ls_project_id, s.completed, c.mission_id
                FROM slate_labels s
                JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
                WHERE s.tenant_id = :t AND NOT s.superseded
                  AND s.ls_project_id = ANY(CAST(:projects AS integer[]))
                ORDER BY s.ls_project_id, s.id{lock("s")}
                """),
            {"t": tenant_id, "projects": [r.ls_project_id for r in records]},
        )
    ).all()
    strangers = sorted({r.ls_project_id for r in labels if r.mission_id != found.id})
    if strangers:
        raise ProjectRetirementRefused(
            f"slate project(s) {', '.join(map(str, strangers))} hold another "
            f"mission's live labels: retiring them would stop its sync too"
        )
    projects = tuple(
        PlannedProject(
            ls_project_id=r.ls_project_id,
            title=r.title,
            placeholders=tuple(
                label.id
                for label in labels
                if label.ls_project_id == r.ls_project_id and not label.completed
            ),
            completed=tuple(
                label.id
                for label in labels
                if label.ls_project_id == r.ls_project_id and label.completed
            ),
        )
        for r in records
    )
    completed = sum(len(p.completed) for p in projects)
    if completed and not allow_completed:
        raise ProjectRetirementRefused(
            f"{completed} completed slate label(s) are live in these projects: a "
            "person's work, never retired silently. Allow completed labels to "
            "keep them live (not superseded, no longer synced)"
        )
    body = {
        "mission": mission_number,
        "reason": reason,
        "projects": [asdict(p) for p in projects],
        "template": (
            None if found.slate_template_id is None else str(found.slate_template_id)
        ),
        "clear_template": clear_template,
    }
    return ProjectRetirementPlan(
        mission_id=found.id,
        mission_number=mission_number,
        reason=reason,
        projects=projects,
        template_id=found.slate_template_id,
        template_name=found.template_name,
        clear_template=clear_template,
        fingerprint=plan_fingerprint(body),
    )


async def apply_project_retirement(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_number: int,
    reason: str,
    *,
    clear_template: bool,
    allow_completed: bool = False,
    expect: str,
) -> ProjectRetirementApplied:
    """Write the reviewed plan, all or nothing, inside the caller's transaction:
    any surprise raises, and the caller's transaction rolls back."""
    plan = await plan_project_retirement(
        conn, tenant_id, mission_number, reason,
        clear_template=clear_template, allow_completed=allow_completed, _lock=True,
    )  # fmt: skip
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the plan is now {plan.fingerprint}, not the reviewed {expect}: "
            "review it again"
        )
    project_ids = [p.ls_project_id for p in plan.projects]
    retired = (
        await conn.execute(
            text("""
                UPDATE label_studio_projects
                SET retired_at = now(), retired_reason = :why
                WHERE tenant_id = :t AND mission_id = :m AND kind = 'slate'
                  AND ls_project_id = ANY(CAST(:ids AS integer[]))
                  AND retired_at IS NULL
                """),
            {"t": tenant_id, "m": plan.mission_id, "ids": project_ids,
             "why": plan.reason},
        )
    ).rowcount  # fmt: skip
    if retired != len(project_ids):
        raise PlanChanged(
            f"retired {retired} projects, not the plan's {len(project_ids)}"
        )
    placeholders = [i for p in plan.projects for i in p.placeholders]
    superseded = (
        await conn.execute(
            text("""
                UPDATE slate_labels SET superseded = true, superseded_reason = :why
                WHERE tenant_id = :t AND id = ANY(CAST(:ids AS uuid[]))
                  AND NOT superseded AND NOT completed
                """),
            {"t": tenant_id, "ids": placeholders, "why": plan.reason},
        )
    ).rowcount
    if superseded != len(placeholders):
        raise PlanChanged(
            f"superseded {superseded} placeholders, not the plan's {len(placeholders)}"
        )
    cleared = False
    if plan.clear_template:
        written = (
            await conn.execute(
                text("""
                    UPDATE missions SET slate_template_id = NULL,
                                        calibration_links_changed_at = now()
                    WHERE tenant_id = :t AND id = :m AND slate_template_id = :old
                    """),
                {"t": tenant_id, "m": plan.mission_id, "old": plan.template_id},
            )
        ).rowcount
        if written != 1:
            raise PlanChanged(
                f"mission {plan.mission_number}'s slate template is no longer "
                f"{plan.template_id}: nothing cleared"
            )
        cleared = True
    return ProjectRetirementApplied(
        projects=retired, placeholders=superseded, template_cleared=cleared
    )
