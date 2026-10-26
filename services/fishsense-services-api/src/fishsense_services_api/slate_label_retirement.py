"""Retiring slate labels on purpose, and withdrawing the calibration built on
them (0077; the owner, 2026-10-26).

Mission 436's slate was held upside down on 11 of its 12 slate frames, and
those labels are mirrored: the affine map from the template's reference
points to the clicks has a negative determinant, which a board seen from its
taped face cannot give. Of all 483 live human slate labels they are the only
ones, and the automatic template fit keeps the template's handedness on all
11. Its production calibration (from v1, on those labels) is withdrawn with
them.

Labels are superseded with a reason, never deleted or moved (``superseded_
reason``; a retired label stays retired, see ``slate_store``). The mission's
current calibration, if accepted, is withdrawn by appending a refused one
after it (calibrations are append-only): the mission, and any mission that
borrows it, then has no effective calibration, and the measurements made with
it leave the current set. The refusal is like any other: the mission comes
back to stage 8 once a slate or laser label on it is newer.

A plan is read-only; its fingerprint covers what it would write and what it
would take away. The apply re-plans under a lock and writes only the plan the
operator reviewed, all or nothing (as ``mission_backfill``).

Runs in a tenant transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_backfill import PlanChanged, plan_fingerprint

__all__ = [
    "Applied",
    "PlannedLabel",
    "RetirementPlan",
    "RetirementRefused",
    "apply_retirement",
    "plan_retirement",
]


class RetirementRefused(ValueError):
    """The labels can't be retired as asked; the message says why."""


@dataclass(frozen=True)
class PlannedLabel:
    label_id: uuid.UUID
    capture_id: uuid.UUID
    capture_number: int
    #: Exactly as reviewed: the apply writes nothing if they changed.
    reference_points: list | None
    skipped_points: list | None


@dataclass(frozen=True)
class RetirementPlan:
    mission_id: uuid.UUID
    mission_number: int
    reason: str
    labels: tuple[PlannedLabel, ...]
    #: The mission's accepted calibration to withdraw; None when it has none.
    calibration_id: uuid.UUID | None
    calibration_producer: str | None
    #: Missions (by number) that borrow that calibration and lose it too.
    borrowers: tuple[int, ...]
    #: Current measurements made with it, here and in its borrowers.
    measurements_leaving: int
    fingerprint: str


@dataclass(frozen=True)
class Applied:
    labels: int
    #: The refused calibration appended; None when there was none to withdraw.
    calibration_id: uuid.UUID | None


async def plan_retirement(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_number: int,
    label_ids: Sequence[uuid.UUID],
    reason: str,
    *,
    _lock: bool = False,
) -> RetirementPlan:
    """What retiring these labels of the mission would do. Read-only."""
    reason = (reason or "").strip()
    if not reason:
        raise RetirementRefused("say why the labels are retired")
    lock = " FOR UPDATE" if _lock else ""
    found = (
        await conn.execute(
            text(f"""
                SELECT id, slate_template_id FROM missions
                WHERE tenant_id = :t AND number = :n{lock}
                """),
            {"t": tenant_id, "n": mission_number},
        )
    ).one_or_none()
    if found is None:
        raise RetirementRefused(f"no mission {mission_number}")
    mission_id = found.id
    wanted = list(dict.fromkeys(label_ids))
    rows = {
        r.id: r
        for r in await conn.execute(
            text(f"""
                SELECT s.id, s.capture_id, c.number AS capture_number,
                       c.mission_id, s.superseded, s.reference_points,
                       s.skipped_points
                FROM slate_labels s
                JOIN captures c ON c.tenant_id = s.tenant_id AND c.id = s.capture_id
                WHERE s.tenant_id = :t AND s.id = ANY(CAST(:ids AS uuid[])){lock}
                """),
            {"t": tenant_id, "ids": wanted},
        )
    }
    for label_id in wanted:
        row = rows.get(label_id)
        if row is None:
            raise RetirementRefused(f"no slate label {label_id}")
        if row.mission_id != mission_id:
            raise RetirementRefused(
                f"slate label {label_id} is not mission {mission_number}'s"
            )
        if row.superseded:
            raise RetirementRefused(f"slate label {label_id} is already superseded")
    labels = tuple(
        PlannedLabel(
            label_id=i,
            capture_id=rows[i].capture_id,
            capture_number=rows[i].capture_number,
            reference_points=rows[i].reference_points,
            skipped_points=rows[i].skipped_points,
        )
        for i in wanted
    )
    current = (
        await conn.execute(
            text("""
                SELECT id, producer FROM current_laser_calibrations
                WHERE tenant_id = :t AND mission_id = :m AND outcome = 'accepted'
                """),
            {"t": tenant_id, "m": mission_id},
        )
    ).one_or_none()
    borrowers: tuple[int, ...] = ()
    leaving = 0
    if current is not None:
        borrowers = tuple(
            await conn.scalars(
                text("""
                    SELECT number FROM missions
                    WHERE tenant_id = :t AND calibration_source_mission_id = :m
                      AND NOT EXISTS (
                          SELECT 1 FROM current_laser_calibrations own
                          WHERE own.tenant_id = missions.tenant_id
                            AND own.mission_id = missions.id
                            AND own.outcome = 'accepted')
                    ORDER BY number
                    """),
                {"t": tenant_id, "m": mission_id},
            )
        )
        leaving = (
            await conn.execute(
                text("""
                    SELECT count(*) FROM current_measurements
                    WHERE tenant_id = :t AND laser_calibration_id = :c
                    """),
                {"t": tenant_id, "c": current.id},
            )
        ).scalar_one()
    body = {
        "mission": mission_number,
        "reason": reason,
        "labels": [asdict(label) for label in labels],
        "calibration": None if current is None else str(current.id),
        "borrowers": list(borrowers),
        "measurements_leaving": leaving,
    }
    return RetirementPlan(
        mission_id=mission_id,
        mission_number=mission_number,
        reason=reason,
        labels=labels,
        calibration_id=None if current is None else current.id,
        calibration_producer=None if current is None else current.producer,
        borrowers=borrowers,
        measurements_leaving=leaving,
        fingerprint=plan_fingerprint(body),
    )


async def apply_retirement(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_number: int,
    label_ids: Sequence[uuid.UUID],
    reason: str,
    *,
    expect: str,
) -> Applied:
    """Write the reviewed plan, all or nothing, inside the caller's transaction."""
    plan = await plan_retirement(
        conn, tenant_id, mission_number, label_ids, reason, _lock=True
    )
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the plan is now {plan.fingerprint}, not the reviewed {expect}: "
            "review it again"
        )
    retired = (
        await conn.execute(
            text("""
                UPDATE slate_labels SET superseded = true, superseded_reason = :why
                WHERE tenant_id = :t AND id = ANY(CAST(:ids AS uuid[]))
                  AND NOT superseded
                """),
            {"t": tenant_id, "ids": [p.label_id for p in plan.labels],
             "why": plan.reason},
        )
    ).rowcount  # fmt: skip
    if retired != len(plan.labels):
        raise PlanChanged(
            f"retired {retired} labels, not the plan's {len(plan.labels)}"
        )
    withdrawn = None
    if plan.calibration_id is not None:
        withdrawn = (
            await conn.execute(
                text("""
                    INSERT INTO laser_calibrations (tenant_id, mission_id, producer,
                        outcome, refusal_reason, inputs_as_of, slate_template_id)
                    SELECT :t, :m, :producer, 'refused', :why, now(), d.slate_template_id
                    FROM missions d WHERE d.tenant_id = :t AND d.id = :m
                    RETURNING id
                    """),
                {
                    "t": tenant_id,
                    "m": plan.mission_id,
                    "producer": plan.calibration_producer,
                    "why": (
                        f"withdrawn by an operator: {len(plan.labels)} of its slate "
                        f"labels were retired ({plan.reason}); calibration "
                        f"{plan.calibration_id} was made from them"
                    ),
                },
            )
        ).scalar_one()
    return Applied(labels=retired, calibration_id=withdrawn)
