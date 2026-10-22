"""Every reviewed frame gets measured (the owner, 2026-10-21).

Stage 14 measures only high-priority missions: a low-priority one is parked.
A frame whose dot or head/tail label came from a review is the exception. It
is work whatever its mission's priority, as long as the human path can
measure it (a species label, and for a real fish its cluster: grouping is
another agent's). On a parked mission, only reviewed frames are work: a
review must not unpark the rest.
"""

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    exec_,
    forget_identities,
    measurable_capture,
    tenant,
)
from sqlalchemy import text
from test_automatic_review_store import _render
from test_measurement_store import _next, _work

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.review_store import ReviewInput, record_review, set_skill


async def _expert(owner_engine, app_engine, lab):
    async with owner_engine.begin() as conn:
        user = (
            await conn.execute(
                text("INSERT INTO users (sub) VALUES ('rev') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO memberships (tenant_id, user_id, role) "
                "VALUES (:t, :u, 'member')"
            ),
            {"t": lab, "u": user},
        )
    async with tenant_transaction(app_engine, lab) as conn:
        for kind in ("laser", "head_tail"):
            await set_skill(conn, lab, user, kind, "expert", set_by=user)
    return user


async def _review_head_tail(owner_engine, app_engine, lab, capture_id, reviewer):
    await _render(owner_engine, lab, capture_id)
    async with owner_engine.connect() as conn:
        render = (
            await conn.execute(
                text("SELECT id FROM capture_renders WHERE capture_id = :c"),
                {"c": capture_id},
            )
        ).scalar_one()
    async with tenant_transaction(app_engine, lab) as conn:
        await record_review(conn, lab, ReviewInput(
            capture_id=capture_id, reviewer_id=reviewer, kind="head_tail", action="fix",
            head=(650.0, 444.0), tail=(890.0, 580.0), render_id=render,
        ))  # fmt: skip


async def test_a_reviewed_frame_on_a_parked_mission_is_measured(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    parked, _ = await calibrated_dive(owner_engine, lab, priority="low")
    reviewed = await measurable_capture(owner_engine, lab, parked)
    reviewer = await _expert(owner_engine, app_engine, lab)
    assert await _next(app_engine, lab) is None

    await _review_head_tail(owner_engine, app_engine, lab, reviewed, reviewer)

    assert await _next(app_engine, lab) == parked
    (work,) = (await _work(app_engine, lab, parked)).captures
    assert work.capture_id == reviewed
    ht = work.head_tail
    assert (ht.head_x, ht.head_y, ht.tail_x, ht.tail_y) == (650.0, 444.0, 890.0, 580.0)


async def test_a_review_does_not_unpark_the_rest_of_the_mission(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    parked, _ = await calibrated_dive(owner_engine, lab, priority="low")
    reviewed = await measurable_capture(owner_engine, lab, parked)
    await measurable_capture(owner_engine, lab, parked)  # not reviewed
    reviewer = await _expert(owner_engine, app_engine, lab)

    await _review_head_tail(owner_engine, app_engine, lab, reviewed, reviewer)

    assert [c.capture_id for c in (await _work(app_engine, lab, parked)).captures] == [
        reviewed
    ]


async def test_a_high_priority_missions_work_is_unchanged(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission, _ = await calibrated_dive(owner_engine, lab)
    reviewed = await measurable_capture(owner_engine, lab, mission)
    other = await measurable_capture(owner_engine, lab, mission)
    reviewer = await _expert(owner_engine, app_engine, lab)

    await _review_head_tail(owner_engine, app_engine, lab, reviewed, reviewer)

    assert {c.capture_id for c in (await _work(app_engine, lab, mission)).captures} == {
        reviewed,
        other,
    }


async def test_the_status_shows_a_parked_missions_reviewed_frames_as_pending(
    owner_engine, app_engine
):
    """`dive_pipeline_status` says what stage 14 will pick (0063)."""
    lab = await tenant(owner_engine)
    parked, _ = await calibrated_dive(owner_engine, lab, priority="low")
    reviewed = await measurable_capture(owner_engine, lab, parked)
    reviewer = await _expert(owner_engine, app_engine, lab)
    assert not await _measurement_pending(app_engine, lab, parked)

    await _review_head_tail(owner_engine, app_engine, lab, reviewed, reviewer)

    assert await _measurement_pending(app_engine, lab, parked)


async def _measurement_pending(app_engine, lab, mission_id) -> bool:
    async with tenant_transaction(app_engine, lab) as conn:
        return (
            await conn.execute(
                text(
                    "SELECT s.measurement_pending FROM dive_pipeline_status s "
                    "JOIN missions m ON m.number = s.dive_id WHERE m.id = :m"
                ),
                {"m": mission_id},
            )
        ).scalar_one()
