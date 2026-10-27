"""Retiring slate labels on purpose, and withdrawing the calibration built on
them (0077; the owner, 2026-10-26: mission 436's 11 mirrored labels).

A plan is read-only and names everything the apply would do: each label (its
exact points, as reviewed), the mission's calibration to withdraw, the
missions that borrow it, and how many current measurements stop counting. Its
fingerprint covers all of it; the apply re-plans under a lock and writes only
that plan, all or nothing. Pinned here:

* the plan lists the labels, the calibration, its borrowers and the
  measurements leaving, and writes nothing;
* the apply supersedes each label with its reason, coordinates untouched,
  and appends a refused calibration after the accepted one, so neither the
  mission nor a borrower has an effective calibration and its measurements
  leave the current set;
* a plan that changed since the review (a label edited) writes nothing;
* a label of another mission, or one already superseded, is refused;
* with no accepted calibration, only the labels are retired.
"""

import uuid

import pytest
from sqlalchemy import text

from _slate_calibration_seed import (
    capture,
    device_with_camera,
    laser_calibration,
    mission,
    slate_label,
    slate_template,
    tenant,
)
from depth_measure_seed import fish, measurement
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mission_backfill import PlanChanged
from fishsense_services_api.slate_label_retirement import (
    RetirementRefused,
    apply_retirement,
    plan_retirement,
)

POINTS = [[[1769.0, 1375.0], [1993.0, 1219.0], [2196.0, 1439.0]],
          [[1812.0, 1253.0], [1975.0, 1449.0], [2179.0, 1315.0]]]  # fmt: skip


async def _mission_number(owner_engine, mission_id) -> int:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("SELECT number FROM missions WHERE id = :m"),
                                   {"m": mission_id})).scalar_one()  # fmt: skip


async def _seeded(owner_engine):
    """A slate mission with two completed labels, an accepted calibration, a
    measurement on it, and a mission that borrows it."""
    lab = await tenant(owner_engine)
    template = await slate_template(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    m = await mission(owner_engine, lab, slate=template, device=device)
    frames = [await capture(owner_engine, lab, m) for _ in POINTS]
    labels = [await slate_label(owner_engine, lab, f, completed=True,
                                reference_points=p, skipped_points=[3])
              for f, p in zip(frames, POINTS)]  # fmt: skip
    calibration = await laser_calibration(owner_engine, lab, m,
                                          slate_template_id=template)  # fmt: skip
    await measurement(owner_engine, lab, frames[0], await fish(owner_engine, lab),
                      calibration)  # fmt: skip
    borrower = await mission(owner_engine, lab, device=device, source=m)
    return lab, m, labels, calibration, borrower


async def _plan(engine, lab, number, labels, reason="mirrored"):
    async with tenant_transaction(engine, lab) as conn:
        return await plan_retirement(conn, lab, number, labels, reason)


async def _apply(engine, lab, number, labels, expect, reason="mirrored"):
    async with tenant_transaction(engine, lab) as conn:
        return await apply_retirement(conn, lab, number, labels, reason, expect=expect)


async def _rows(owner_engine, sql, **params):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def test_the_plan_names_everything_and_writes_nothing(owner_engine):
    lab, m, labels, calibration, borrower = await _seeded(owner_engine)
    number = await _mission_number(owner_engine, m)

    plan = await _plan(owner_engine, lab, number, labels)

    assert [p.label_id for p in plan.labels] == labels
    assert [p.reference_points for p in plan.labels] == POINTS
    assert plan.calibration_id == calibration
    assert plan.borrowers == (await _mission_number(owner_engine, borrower),)
    assert plan.measurements_leaving == 1
    assert (
        plan.fingerprint == (await _plan(owner_engine, lab, number, labels)).fingerprint
    )
    assert (
        await _rows(owner_engine, "SELECT 1 FROM slate_labels WHERE superseded") == []
    )


async def test_the_apply_retires_the_labels_and_withdraws_the_calibration(
    owner_engine,
):
    lab, m, labels, calibration, borrower = await _seeded(owner_engine)
    number = await _mission_number(owner_engine, m)
    plan = await _plan(owner_engine, lab, number, labels)

    applied = await _apply(owner_engine, lab, number, labels, plan.fingerprint)

    assert applied.labels == 2
    rows = await _rows(owner_engine, "SELECT id, superseded, superseded_reason, "
                       "reference_points FROM slate_labels ORDER BY number")  # fmt: skip
    assert [(r.superseded, r.superseded_reason) for r in rows] == [
        (True, "mirrored")
    ] * 2
    assert [r.reference_points for r in rows] == POINTS  # never moved
    (current,) = await _rows(owner_engine, "SELECT id, outcome, producer, "
                             "refusal_reason FROM current_laser_calibrations "
                             "WHERE mission_id = :m", m=m)  # fmt: skip
    assert current.id == applied.calibration_id != calibration
    assert (current.outcome, current.producer) == ("refused", "slate")
    assert "mirrored" in current.refusal_reason
    assert await _rows(owner_engine, "SELECT 1 FROM effective_laser_calibrations "
                       "WHERE mission_id IN (:m, :b)", m=m, b=borrower) == []  # fmt: skip
    assert await _rows(owner_engine, "SELECT 1 FROM current_measurements") == []


async def test_a_plan_that_changed_since_the_review_writes_nothing(owner_engine):
    lab, m, labels, _, _ = await _seeded(owner_engine)
    number = await _mission_number(owner_engine, m)
    plan = await _plan(owner_engine, lab, number, labels)
    async with owner_engine.begin() as conn:  # a sync edits a label meanwhile
        await conn.execute(text("UPDATE slate_labels SET reference_points = "
                                "'[[1, 2], [3, 4], [5, 6]]' WHERE id = :i"),
                           {"i": labels[0]})  # fmt: skip

    with pytest.raises(PlanChanged):
        await _apply(owner_engine, lab, number, labels, plan.fingerprint)

    assert (
        await _rows(owner_engine, "SELECT 1 FROM slate_labels WHERE superseded") == []
    )
    assert len(await _rows(owner_engine, "SELECT 1 FROM laser_calibrations")) == 1


async def test_a_label_of_another_mission_or_already_superseded_is_refused(
    owner_engine,
):
    lab, m, labels, _, _ = await _seeded(owner_engine)
    number = await _mission_number(owner_engine, m)
    other = await mission(owner_engine, lab)
    stranger = await slate_label(owner_engine, lab, await capture(owner_engine, lab, other),
                                 completed=True)  # fmt: skip
    gone = await slate_label(owner_engine, lab, await capture(owner_engine, lab, m),
                             completed=True, superseded=True)  # fmt: skip

    for extra in (stranger, gone, uuid.uuid4()):
        with pytest.raises(RetirementRefused):
            await _plan(owner_engine, lab, number, [*labels, extra])
    with pytest.raises(RetirementRefused):
        await _plan(owner_engine, lab, number, labels, reason=" ")


async def test_without_an_accepted_calibration_only_the_labels_are_retired(
    owner_engine,
):
    lab = await tenant(owner_engine)
    m = await mission(owner_engine, lab, slate=await slate_template(owner_engine))
    label = await slate_label(owner_engine, lab, await capture(owner_engine, lab, m),
                              completed=True, reference_points=POINTS[0])  # fmt: skip
    number = await _mission_number(owner_engine, m)
    plan = await _plan(owner_engine, lab, number, [label])

    assert plan.calibration_id is None
    applied = await _apply(owner_engine, lab, number, [label], plan.fingerprint)

    assert (applied.labels, applied.calibration_id) == (1, None)
    assert await _rows(owner_engine, "SELECT 1 FROM laser_calibrations") == []
