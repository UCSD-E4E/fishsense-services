"""Retiring a mission's slate Label Studio project on purpose (0079; the
owner, 2026-10-26).

``assign-slate-templates`` gave mission 508, a pool test that must never have
a dive slate, a template from an automatic vote, and stage 9 made it a slate
project (300745) of 47 placeholders; mission 439 got one too (300747, 9
placeholders) that the owner does not want labeled now. Pinned here:

* the plan names each live slate project of the mission, its placeholders
  (not completed, not superseded) and its completed labels, and the template,
  and writes nothing;
* the apply retires the projects with the reason, supersedes the
  placeholders with it (settled: stage 9 does not draw those frames again),
  and, when asked, clears the template on its exact old value and stamps
  ``calibration_links_changed_at``; no row is deleted, no point moved;
* a completed label is a person's work: the plan is refused unless
  ``allow_completed``, and even then the label stays live;
* a plan that changed since the review writes nothing;
* a template that changed since the plan is not cleared;
* no reason, no project, no template to clear, or a project holding another
  mission's labels, is refused.
"""

import pytest
from sqlalchemy import text

from _slate_calibration_seed import (
    capture,
    device_with_camera,
    ls_project,
    mission,
    slate_label,
    slate_template,
    species,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mission_backfill import PlanChanged
from fishsense_services_api.slate_project_retirement import (
    ProjectRetirementRefused,
    apply_project_retirement,
    plan_project_retirement,
)
from fishsense_services_api.slate_store import (
    next_mission_for_slate_preprocessing,
    slate_label_studio_projects,
)

POINTS = [[1769.0, 1375.0], [1993.0, 1219.0], [2196.0, 1439.0]]
REASON = "pool test: never a dive slate"


async def _number(owner_engine, mission_id) -> int:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("SELECT number FROM missions WHERE id = :m"),
                                   {"m": mission_id})).scalar_one()  # fmt: skip


async def _rows(owner_engine, sql, **params):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def _seeded(owner_engine, *, completed: int = 0):
    """Mission 508's shape: a slate template, marked slate frames, and a slate
    project of placeholders (plus `completed` labeled ones)."""
    lab = await tenant(owner_engine)
    template = await slate_template(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    m = await mission(owner_engine, lab, slate=template, device=device)
    await ls_project(owner_engine, lab, m, 300745, title="Pool #508 - Slate")
    placeholders, done = [], []
    for i in range(3 + completed):
        frame = await capture(owner_engine, lab, m)
        await species(owner_engine, lab, frame)
        label = await slate_label(
            owner_engine, lab, frame, project=300745, completed=i >= 3,
            reference_points=POINTS if i >= 3 else None,
        )  # fmt: skip
        (done if i >= 3 else placeholders).append(label)
    # Already superseded (dead-lettered): neither a placeholder nor work.
    await slate_label(owner_engine, lab, await capture(owner_engine, lab, m),
                      project=300745, superseded=True)  # fmt: skip
    return lab, m, template, placeholders, done


async def _plan(engine, lab, number, *, clear=True, allow=False, reason=REASON):
    async with tenant_transaction(engine, lab) as conn:
        return await plan_project_retirement(
            conn, lab, number, reason, clear_template=clear, allow_completed=allow
        )


async def _apply(engine, lab, number, expect, *, clear=True, allow=False):
    async with tenant_transaction(engine, lab) as conn:
        return await apply_project_retirement(
            conn, lab, number, REASON, clear_template=clear, allow_completed=allow,
            expect=expect,
        )  # fmt: skip


async def test_the_plan_names_everything_and_writes_nothing(owner_engine):
    lab, m, template, placeholders, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)

    plan = await _plan(owner_engine, lab, number)

    (project,) = plan.projects
    assert (project.ls_project_id, project.title) == (300745, "Pool #508 - Slate")
    assert sorted(project.placeholders) == sorted(placeholders)
    assert project.completed == ()
    assert (plan.template_id, plan.clear_template) == (template, True)
    assert plan.template_name.startswith("H-Slate")
    assert plan.fingerprint == (await _plan(owner_engine, lab, number)).fingerprint
    assert (
        plan.fingerprint
        != (await _plan(owner_engine, lab, number, clear=False)).fingerprint
    )
    assert await _rows(owner_engine, "SELECT 1 FROM label_studio_projects "
                       "WHERE retired_at IS NOT NULL") == []  # fmt: skip
    assert len(await _rows(owner_engine, "SELECT 1 FROM slate_labels "
                           "WHERE superseded")) == 1  # fmt: skip


async def test_the_apply_retires_the_project_its_placeholders_and_template(
    owner_engine, app_engine
):
    lab, m, template, placeholders, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)
    before = await _rows(owner_engine, "SELECT calibration_links_changed_at "
                         "FROM missions WHERE id = :m", m=m)  # fmt: skip
    plan = await _plan(owner_engine, lab, number)

    applied = await _apply(owner_engine, lab, number, plan.fingerprint)

    assert (applied.projects, applied.placeholders, applied.template_cleared) == (
        1, 3, True,
    )  # fmt: skip
    (project,) = await _rows(owner_engine, "SELECT retired_at, retired_reason "
                             "FROM label_studio_projects")  # fmt: skip
    assert project.retired_at is not None and project.retired_reason == REASON
    rows = await _rows(owner_engine, "SELECT id, superseded, superseded_reason "
                       "FROM slate_labels WHERE id = ANY(:ids)",
                       ids=placeholders)  # fmt: skip
    assert {(r.superseded, r.superseded_reason) for r in rows} == {(True, REASON)}
    assert len(await _rows(owner_engine, "SELECT 1 FROM slate_labels")) == 4
    (after,) = await _rows(owner_engine, "SELECT slate_template_id, "
                           "calibration_links_changed_at FROM missions "
                           "WHERE id = :m", m=m)  # fmt: skip
    assert after.slate_template_id is None
    assert after.calibration_links_changed_at is not None
    assert after.calibration_links_changed_at != before[0][0]
    async with tenant_transaction(app_engine, lab) as conn:
        assert await slate_label_studio_projects(conn, lab) == []


async def test_without_clearing_the_template_stage_9_does_not_redraw(
    owner_engine, app_engine
):
    """Mission 439: not labeled now, the template kept. Its placeholders are
    retired with a reason, so the frames stay settled."""
    lab, m, template, _, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)
    plan = await _plan(owner_engine, lab, number, clear=False)

    applied = await _apply(owner_engine, lab, number, plan.fingerprint, clear=False)

    assert applied.template_cleared is False
    assert (await _rows(owner_engine, "SELECT slate_template_id FROM missions "
                        "WHERE id = :m", m=m))[0][0] == template  # fmt: skip
    async with tenant_transaction(app_engine, lab) as conn:
        assert await next_mission_for_slate_preprocessing(conn, lab) is None


async def test_completed_labels_are_refused_unless_allowed_and_kept(owner_engine):
    lab, m, _, placeholders, done = await _seeded(owner_engine, completed=2)
    number = await _number(owner_engine, m)

    with pytest.raises(ProjectRetirementRefused, match="2 completed"):
        await _plan(owner_engine, lab, number)

    plan = await _plan(owner_engine, lab, number, allow=True)
    assert sorted(plan.projects[0].completed) == sorted(done)
    with pytest.raises(ProjectRetirementRefused, match="2 completed"):
        await _apply(owner_engine, lab, number, plan.fingerprint)
    applied = await _apply(owner_engine, lab, number, plan.fingerprint, allow=True)

    assert applied.placeholders == 3
    rows = await _rows(owner_engine, "SELECT superseded, superseded_reason, "
                       "reference_points FROM slate_labels WHERE id = ANY(:ids)",
                       ids=done)  # fmt: skip
    assert [(r.superseded, r.superseded_reason) for r in rows] == [(False, None)] * 2
    assert [r.reference_points for r in rows] == [POINTS] * 2  # never moved


async def test_a_plan_that_changed_since_the_review_writes_nothing(owner_engine):
    lab, m, _, placeholders, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)
    plan = await _plan(owner_engine, lab, number)
    # Stage 9 adds a frame meanwhile.
    await slate_label(owner_engine, lab, await capture(owner_engine, lab, m),
                      project=300745)  # fmt: skip

    with pytest.raises(PlanChanged):
        await _apply(owner_engine, lab, number, plan.fingerprint)

    assert await _rows(owner_engine, "SELECT 1 FROM label_studio_projects "
                       "WHERE retired_at IS NOT NULL") == []  # fmt: skip
    assert len(await _rows(owner_engine, "SELECT 1 FROM slate_labels "
                           "WHERE superseded")) == 1  # fmt: skip


async def test_the_template_is_cleared_only_on_its_exact_old_value(owner_engine):
    """The UPDATE matches the plan's template id: a template changed under the
    apply (past the re-plan) rolls everything back."""
    lab, m, template, _, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)
    other = await slate_template(owner_engine)
    plan = await _plan(owner_engine, lab, number)

    with pytest.raises(PlanChanged, match="template"):
        async with tenant_transaction(owner_engine, lab) as conn:
            await conn.execute(text("""
            CREATE FUNCTION pg_temp.swap() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE missions SET slate_template_id = CAST(TG_ARGV[0] AS uuid)
                WHERE id = CAST(TG_ARGV[1] AS uuid);
                RETURN NULL;
            END $$;
            """))  # fmt: skip
            await conn.execute(text(
            f"CREATE TRIGGER swap AFTER UPDATE ON label_studio_projects "
            f"FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.swap('{other}', '{m}')"
        ))  # fmt: skip
            await apply_project_retirement(
                conn, lab, number, REASON, clear_template=True, expect=plan.fingerprint
            )

    (row,) = await _rows(owner_engine, "SELECT slate_template_id FROM missions "
                         "WHERE id = :m", m=m)  # fmt: skip
    assert row.slate_template_id == template
    assert await _rows(owner_engine, "SELECT 1 FROM label_studio_projects "
                       "WHERE retired_at IS NOT NULL") == []  # fmt: skip


async def test_what_cannot_be_retired_is_refused(owner_engine):
    lab, m, _, _, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)
    bare = await _number(owner_engine, await mission(owner_engine, lab))
    templateless = await mission(owner_engine, lab)
    await ls_project(owner_engine, lab, templateless, 300800)
    retired = await mission(owner_engine, lab, slate=await slate_template(owner_engine))
    await ls_project(owner_engine, lab, retired, 300801, retired_reason="earlier")

    for args, kwargs, why in (
        ((number,), {"reason": " "}, "say why"),
        ((9999,), {}, "no mission 9999"),
        ((bare,), {"clear": False}, "no slate project"),
        ((await _number(owner_engine, retired),), {}, "no slate project"),
        ((await _number(owner_engine, templateless),), {}, "no slate template"),
    ):
        with pytest.raises(ProjectRetirementRefused, match=why):
            await _plan(owner_engine, lab, *args, **kwargs)


async def test_a_project_holding_another_missions_labels_is_refused(owner_engine):
    """A v1 shared project: retiring it would stop the sync for another
    mission's labels too."""
    lab, m, _, _, _ = await _seeded(owner_engine)
    other = await mission(owner_engine, lab)
    await slate_label(owner_engine, lab, await capture(owner_engine, lab, other),
                      project=300745)  # fmt: skip

    with pytest.raises(ProjectRetirementRefused, match="another mission"):
        await _plan(owner_engine, lab, await _number(owner_engine, m))
