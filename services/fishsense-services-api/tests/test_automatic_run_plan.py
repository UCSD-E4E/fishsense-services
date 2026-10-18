"""Which named dives a by-hand automatic run would take, and in what order.

New in v2 (Phase C, 2026-10-18). The validation run names the paper's dives
by number (cscw-fishsense2027 §6): calibration sessions first -- their slate
frames feed the label-free fits their borrowers use -- then the rest in the
order given. The plan is what the operator reviews before anything runs: each
dive's folder, whether it is a session, excluded or human-measured, and how
many frames it still owes. A number with no dive is reported, not skipped.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    capture,
    device,
    dive,
    exec_,
    fish,
    forget_identities,
    measurable_capture,
    measurement,
    tenant,
)
from fishsense_services_api.automatic_results_store import (
    AutomaticHeadTailRow,
    automatic_run_plan,
    persist_automatic_head_tails,
)
from fishsense_services_api.db import tenant_transaction

HV = 2


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _number(owner_engine, dive_id) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT number FROM dives WHERE id = :d"), {"d": dive_id}
            )
        ).scalar_one()


async def _dive(owner_engine, lab, **kwargs):
    return await dive(owner_engine, lab, device_id=await device(owner_engine, lab),
                      priority="low", **kwargs)  # fmt: skip


async def test_sessions_run_first_then_the_order_given(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    session = await _dive(owner_engine, lab)
    fish_a = await _dive(owner_engine, lab, source_dive=session)
    reef = await _dive(owner_engine, lab)
    numbers = [await _number(owner_engine, d) for d in (reef, fish_a, session)]

    plan = await _in(app_engine, lab, automatic_run_plan, numbers, headtail_version=HV)

    assert [p.dive_id for p in plan.dives] == [session, reef, fish_a]
    assert [p.calibration_session for p in plan.dives] == [True, False, False]
    assert plan.missing == []


async def test_a_number_with_no_dive_is_reported(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    d = await _dive(owner_engine, lab)
    n = await _number(owner_engine, d)

    plan = await _in(
        app_engine, lab, automatic_run_plan, [n, 999_999], headtail_version=HV
    )

    assert [p.number for p in plan.dives] == [n]
    assert plan.missing == [999_999]


async def test_the_plan_counts_the_frames_still_owed(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    d = await _dive(owner_engine, lab)
    done, _, _ = [await capture(owner_engine, lab, d) for _ in range(3)]
    async with tenant_transaction(app_engine, lab) as conn:
        await persist_automatic_head_tails(conn, lab, d, [
            AutomaticHeadTailRow(capture_id=done, status="no_laser_dot",
                                 predictor_version=HV)])  # fmt: skip

    (p,) = (await _in(app_engine, lab, automatic_run_plan,
                      [await _number(owner_engine, d)], headtail_version=HV)).dives  # fmt: skip

    assert (p.frames, p.frames_owed) == (3, 2)
    assert p.folder.startswith("/dives/")


async def test_the_plan_says_which_dives_are_excluded_or_human_measured(
    owner_engine, app_engine
):
    """By hand, an excluded dive still runs (0039): the validation names
    its own dives. The plan says so, so the operator sees it."""
    lab = await tenant(owner_engine)
    excluded = await _dive(owner_engine, lab)
    await exec_(owner_engine, "INSERT INTO automatic_results_exclusions "
                "(tenant_id, dive_id, reason) VALUES (:t, :d, 'test')",
                t=lab, d=excluded)  # fmt: skip
    measured, cal = await calibrated_dive(owner_engine, lab, priority="low")
    c = await measurable_capture(owner_engine, lab, measured)
    await measurement(owner_engine, lab, c, await fish(owner_engine, lab), cal)

    plan = await _in(app_engine, lab, automatic_run_plan,
                     [await _number(owner_engine, excluded),
                      await _number(owner_engine, measured)], headtail_version=HV)  # fmt: skip

    by_id = {p.dive_id: p for p in plan.dives}
    assert by_id[excluded].excluded and not by_id[excluded].human_measured
    assert by_id[measured].human_measured and not by_id[measured].excluded


async def test_the_catalog_plans_for_a_member_tenant_named_by_slug(
    app_engine, seed_memberships
):
    from fishsense_services_api.automatic_results_store import AutomaticResultsCatalog

    sub = "service:fishsense-orchestrator"
    tenants = await seed_memberships({sub: {"lab": "member"}})
    catalog = AutomaticResultsCatalog(app_engine, sub=sub)

    assert await catalog.tenant_by_slug("lab") == tenants["lab"]
    assert await catalog.tenant_by_slug("nobody") is None
    plan = await catalog.automatic_run_plan(tenants["lab"], [1], headtail_version=HV)
    assert plan.dives == [] and plan.missing == [1]
