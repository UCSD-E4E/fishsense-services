"""Recording which slate template a mission used (the owner, 2026-10-26: the
choices for missions with none go into mission metadata, from the owner's
visual labels of the V-Slate variants and the automatic TTT identification).

A plan is read-only and names everything the apply would do and set off: the
missions it assigns, those already on the same template (unchanged), those
on another (a conflict, left alone), and which missions would then enter the
stage-9 slate-preprocessing cohort (new Label Studio work), the stage-13
laser-calibration cohort, and the checkerboard cohort. Its fingerprint covers
all of it; the apply re-plans under a lock and writes only that plan, all or
nothing. Pinned here:

* the plan lists assignments, unchanged missions, conflicts and the side
  effects, and writes nothing;
* the apply sets exactly the plan's templates, and stamps
  ``calibration_links_changed_at`` on each, as v2's own slate setter
  (``species_store.set_mission_slate_template``, v1's ``set_dive_slate``)
  does: a standing refusal is outlived, so a mission whose checkerboard fit
  was refused comes back to that cohort -- and the plan says so;
* a plan that changed since the review writes nothing;
* an unknown mission or template, or a malformed mapping, is refused.
"""

import pytest
from sqlalchemy import text

from _slate_calibration_seed import (
    T0,
    calibration_target,
    capture,
    device_with_camera,
    laser_calibration,
    laser_label,
    mission,
    run,
    slate_label,
    slate_template,
    species,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_calibration_store import (
    next_mission_for_checkerboard_calibration,
    next_mission_for_laser_calibration,
)
from fishsense_services_api.mission_backfill import PlanChanged
from fishsense_services_api.slate_store import next_mission_for_slate_preprocessing
from fishsense_services_api.slate_template_assignment import (
    AssignmentRefused,
    apply_assignment,
    plan_assignment,
)


async def _number(owner_engine, mission_id) -> int:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("SELECT number FROM missions WHERE id = :m"),
                                   {"m": mission_id})).scalar_one()  # fmt: skip


async def _rows(owner_engine, sql, **params):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def _seeded(owner_engine):
    """Six missions and two templates (V-Slate A, v1 id 7; V-Slate B):

    * ``marked``: no template, one canonical frame marked `Slate, Laser on
      slate` and unlabeled -- stage-9 work once it has a template;
    * ``plain``: no template, nothing to do;
    * ``same``: already on A; ``other``: already on B;
    * ``labelled``: no template, two completed slate labels on laser-dotted
      frames -- stage 13 can calibrate it once it has a template;
    * ``refused``: no template, a board, two dotted frames and a refused
      checkerboard fit standing -- out of every cohort until a link changes.
    """
    lab = await tenant(owner_engine)
    a = await slate_template(owner_engine)
    b = await slate_template(owner_engine)
    # reference data outlives a test: the last test's A gives up its v1 id
    await run(owner_engine, "UPDATE slate_templates SET v1_id = NULL WHERE v1_id = 7")
    await run(owner_engine, "UPDATE slate_templates SET v1_id = 7 WHERE id = :i", i=a)
    names = {
        i: (await _rows(owner_engine, "SELECT name FROM slate_templates "
                        "WHERE id = :i", i=i))[0].name  # fmt: skip
        for i in (a, b)
    }
    device, _ = await device_with_camera(owner_engine, lab)
    marked = await mission(owner_engine, lab, device=device)
    await species(owner_engine, lab, await capture(owner_engine, lab, marked))
    plain = await mission(owner_engine, lab, device=device)
    same = await mission(owner_engine, lab, device=device, slate=a)
    other = await mission(owner_engine, lab, device=device, slate=b)
    labelled = await mission(owner_engine, lab, device=device)
    for _ in range(2):
        frame = await capture(owner_engine, lab, labelled)
        await laser_label(owner_engine, lab, frame)
        await slate_label(owner_engine, lab, frame, completed=True,
                          reference_points=[[1.0, 2.0]] * 4)  # fmt: skip
    board = await calibration_target(owner_engine)
    refused = await mission(owner_engine, lab, device=device, target=board)
    for _ in range(2):
        await laser_label(owner_engine, lab, await capture(owner_engine, lab, refused))
    await laser_calibration(owner_engine, lab, refused, outcome="refused",
                            producer="checkerboard", inputs_as_of=T0,
                            calibration_target_id=board)  # fmt: skip
    ids = dict(marked=marked, plain=plain, same=same, other=other,
               labelled=labelled, refused=refused)  # fmt: skip
    numbers = {k: await _number(owner_engine, v) for k, v in ids.items()}
    return lab, a, b, names, ids, numbers


def _mapping(numbers, names):
    return {
        "source": "the owner's visual labels, 2026-10-26",
        "missions": {
            str(numbers["marked"]): {"template_v1": 7},
            str(numbers["plain"]): {"template": names["b"]},
            str(numbers["same"]): {"template_v1": 7},
            str(numbers["other"]): {"template_v1": 7},
            str(numbers["labelled"]): {"template_v1": 7},
            str(numbers["refused"]): {"template": names["a"]},
        },
    }


async def _setup(owner_engine):
    lab, a, b, names, ids, numbers = await _seeded(owner_engine)
    mapping = _mapping(numbers, {"a": names[a], "b": names[b]})
    return lab, a, b, ids, numbers, mapping


async def _plan(engine, lab, mapping):
    async with tenant_transaction(engine, lab) as conn:
        return await plan_assignment(conn, lab, mapping)


async def _apply(engine, lab, mapping, expect):
    async with tenant_transaction(engine, lab) as conn:
        return await apply_assignment(conn, lab, mapping, expect=expect)


async def _templates(owner_engine):
    rows = await _rows(owner_engine, "SELECT id, slate_template_id, "
                       "calibration_links_changed_at FROM missions")  # fmt: skip
    return {r.id: (r.slate_template_id, r.calibration_links_changed_at) for r in rows}


async def test_the_plan_names_everything_and_writes_nothing(owner_engine):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    before = await _templates(owner_engine)

    plan = await _plan(owner_engine, lab, mapping)

    assert [(x.mission_number, x.template_id) for x in plan.assign] == sorted(
        [(n["marked"], a), (n["plain"], b), (n["labelled"], a), (n["refused"], a)]
    )
    assert plan.unchanged == (n["same"],)
    assert [(c.mission_number, c.current_template_id, c.wanted_template_id)
            for c in plan.conflicts] == [(n["other"], b, a)]  # fmt: skip
    # Stage 9: one new frame for Label Studio, on the marked mission.
    assert [(w.mission_number, w.frames) for w in plan.slate_preprocessing] == [
        (n["marked"], 1)
    ]
    assert plan.laser_calibration == (n["labelled"],)
    # The stamp outlives the refused checkerboard fit, so it is tried again.
    assert plan.checkerboard_entering == (n["refused"],)
    assert plan.checkerboard_leaving == ()
    assert plan.source == "the owner's visual labels, 2026-10-26"
    assert plan.fingerprint == (await _plan(owner_engine, lab, mapping)).fingerprint
    assert await _templates(owner_engine) == before


async def test_the_apply_writes_exactly_the_plan_and_stamps_the_link_change(
    owner_engine,
):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    before = await _templates(owner_engine)
    plan = await _plan(owner_engine, lab, mapping)

    applied = await _apply(owner_engine, lab, mapping, plan.fingerprint)

    assert applied.missions == 4
    after = await _templates(owner_engine)
    wanted = {"marked": a, "plain": b, "labelled": a, "refused": a}
    for key, template in wanted.items():
        assert after[ids[key]][0] == template
        # as v2's own slate setter does: a standing refusal is outlived
        assert after[ids[key]][1] is not None
    for key in ("same", "other"):
        assert after[ids[key]] == before[ids[key]]
    # and what the plan said would happen, does: the real selectors agree
    async with tenant_transaction(owner_engine, lab) as conn:
        assert (await next_mission_for_slate_preprocessing(conn, lab)).mission_id == (
            ids["marked"]
        )
        assert (await next_mission_for_laser_calibration(conn, lab)).mission_id == (
            ids["labelled"]
        )
        assert (
            await next_mission_for_checkerboard_calibration(conn, lab)
        ).mission_id == ids["refused"]
    assert (await _plan(owner_engine, lab, mapping)).assign == ()


async def test_a_plan_that_changed_since_the_review_writes_nothing(owner_engine):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    plan = await _plan(owner_engine, lab, mapping)
    # the species sync names a slate on one of them meanwhile
    await run(owner_engine, "UPDATE missions SET slate_template_id = :b WHERE id = :m",
              b=b, m=ids["marked"])  # fmt: skip
    before = await _templates(owner_engine)

    with pytest.raises(PlanChanged):
        await _apply(owner_engine, lab, mapping, plan.fingerprint)

    assert await _templates(owner_engine) == before


async def test_a_wrong_fingerprint_writes_nothing(owner_engine):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    before = await _templates(owner_engine)

    with pytest.raises(PlanChanged):
        await _apply(owner_engine, lab, mapping, "0" * 16)

    assert await _templates(owner_engine) == before


async def test_a_row_that_moved_under_the_apply_rolls_it_back(
    owner_engine, monkeypatch
):
    """The guard on the write itself: only a mission still without a template
    is set, and a short count rolls the whole apply back."""
    from fishsense_services_api import slate_template_assignment as module

    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    plan = await _plan(owner_engine, lab, mapping)
    before = await _templates(owner_engine)
    real = module.plan_assignment

    async def stale(conn, tenant_id, mapping, *, _lock=False):
        result = await real(conn, tenant_id, mapping, _lock=_lock)
        await conn.execute(text("UPDATE missions SET slate_template_id = :b "
                                "WHERE id = :m"), {"b": b, "m": ids["plain"]})  # fmt: skip
        return result

    monkeypatch.setattr(module, "plan_assignment", stale)
    with pytest.raises(PlanChanged):
        await _apply(owner_engine, lab, mapping, plan.fingerprint)

    assert await _templates(owner_engine) == before


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"template_v1": 999999}, "no slate template with v1 id 999999"),
        ({"template": "Q-Slate"}, "no slate template named 'Q-Slate'"),
        ({}, "exactly one of template_v1"),
        ({"template_v1": 7, "template": "x"}, "exactly one of template_v1"),
        ({"template_v1": "7"}, "exactly one of template_v1"),
    ],
)
async def test_an_unknown_template_is_refused(owner_engine, entry, message):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    mapping["missions"][str(n["plain"])] = entry

    with pytest.raises(AssignmentRefused, match=message):
        await _plan(owner_engine, lab, mapping)


async def test_an_unknown_mission_is_refused(owner_engine):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    mapping["missions"]["9999"] = {"template_v1": 7}

    with pytest.raises(AssignmentRefused, match="no mission 9999"):
        await _plan(owner_engine, lab, mapping)


@pytest.mark.parametrize(
    "mapping, message",
    [
        ({"missions": {"1": {"template_v1": 7}}}, "say where"),
        ({"source": " ", "missions": {"1": {"template_v1": 7}}}, "say where"),
        ({"source": "x", "missions": {}}, "no missions"),
        ({"source": "x", "missions": {"one": {"template_v1": 7}}}, "'one'"),
        ({"source": "x", "missions": [1]}, "no missions"),
    ],
)
async def test_a_malformed_mapping_is_refused(owner_engine, mapping, message):
    lab = await tenant(owner_engine)

    with pytest.raises(AssignmentRefused, match=message):
        await _plan(owner_engine, lab, mapping)


async def test_a_mission_of_another_tenant_is_unknown(owner_engine):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    other = await tenant(owner_engine, "partner")

    with pytest.raises(AssignmentRefused, match="no mission"):
        await _plan(owner_engine, other, {"source": "x", "missions": {
            str(n["plain"]): {"template_v1": 7}}})  # fmt: skip
