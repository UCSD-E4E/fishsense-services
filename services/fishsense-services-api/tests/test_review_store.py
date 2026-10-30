"""Reviews and reviewers' skills (0063; the owner, 2026-10-21).

A member reviews one kind of a frame's automatic result -- the laser dot, the
head and tail, or the species -- by accepting it, fixing it, or rejecting it
with a reason. Their skill in that kind decides what happens:

* a Trusted or Expert review counts at once: a dot or head/tail becomes the
  frame's human label, superseding (never deleting) the labels before it;
* a Learning review waits. It counts when a Trusted or Expert reviewer
  confirms it, or a second Learning reviewer agrees with it (the same points
  within ``AGREE_PX``). What becomes the label is the first review's points,
  exactly as placed: never an average;
* a species review is recorded only, until frames are grouped into fish.

The points must be on the frame's render under its device's current camera
calibration: the measurement uses that camera.
"""

import pytest
from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    capture,
    exec_,
    forget_identities,
    head_tail_label,
    laser_label,
)
from sqlalchemy import text
from test_automatic_review_store import _render

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.review_store import (
    AGREE_PX,
    ReviewInput,
    ReviewRefused,
    capture_reviews,
    record_review,
    reviewer_level,
    set_skill,
)

EXPERT, TRUSTED, LEARNER, LEARNER_2, ADMIN = (
    "expert",
    "trusted",
    "learner",
    "learner-2",
    "admin",
)
HEAD, TAIL = (650.0, 444.0), (890.0, 580.0)


@pytest.fixture
async def lab(seed_memberships):
    found = await seed_memberships(
        {s: {"lab": "admin" if s == ADMIN else "member"}
         for s in (EXPERT, TRUSTED, LEARNER, LEARNER_2, ADMIN)}
    )  # fmt: skip
    return found["lab"]


async def _user(owner_engine, sub):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT id FROM users WHERE sub = :s"), {"s": sub})
        ).scalar_one()


@pytest.fixture
async def people(lab, owner_engine, app_engine):
    ids = {
        s: await _user(owner_engine, s)
        for s in (EXPERT, TRUSTED, LEARNER, LEARNER_2, ADMIN)
    }
    async with tenant_transaction(app_engine, lab) as conn:
        for kind in ("laser", "head_tail", "species"):
            await set_skill(conn, lab, ids[EXPERT], kind, "expert", set_by=ids[ADMIN])
            await set_skill(conn, lab, ids[TRUSTED], kind, "trusted", set_by=ids[ADMIN])
    return ids


@pytest.fixture
async def frame(lab, owner_engine):
    """A Lite frame with a Label Studio dot and head/tail, and its render."""
    mission_id, _ = await calibrated_dive(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await laser_label(owner_engine, lab, c, x=770.0, y=524.0)
    await head_tail_label(
        owner_engine, lab, c, head=(600.0, 440.0), tail=(990.0, 600.0)
    )
    await _render(owner_engine, lab, c)
    async with owner_engine.connect() as conn:
        render = (
            await conn.execute(
                text("SELECT id FROM capture_renders WHERE capture_id = :c"), {"c": c}
            )
        ).scalar_one()
    return c, render


async def _review(app_engine, lab, **fields):
    async with tenant_transaction(app_engine, lab) as conn:
        return await record_review(conn, lab, ReviewInput(**fields))


def _head_tail(people, who, frame, *, action="fix", head=HEAD, tail=TAIL, **extra):
    c, render = frame
    return dict(capture_id=c, reviewer_id=people[who], kind="head_tail", action=action,
                head=head, tail=tail, render_id=render, **extra)  # fmt: skip


async def _labels(owner_engine, c, table="head_tail_labels"):
    cols = "head_x, head_y, tail_x, tail_y" if table == "head_tail_labels" else "x, y"
    async with owner_engine.connect() as conn:
        return [
            dict(r._mapping)
            for r in await conn.execute(
                text(f"""
                    SELECT source, superseded, superseded_reason, review_id, {cols}
                    FROM {table} WHERE capture_id = :c ORDER BY number
                    """),
                {"c": c},
            )
        ]


async def test_an_experts_fix_becomes_the_label_and_supersedes_the_old_one(
    lab, people, frame, app_engine, owner_engine
):
    recorded = await _review(app_engine, lab, **_head_tail(people, EXPERT, frame))

    assert (recorded.level, recorded.counts) == ("expert", True)
    old, new = await _labels(owner_engine, frame[0])
    assert (old["superseded"], old["superseded_reason"]) == (True, "review")
    assert (new["source"], new["superseded"], new["review_id"]) == (
        "review",
        False,
        recorded.review_id,
    )
    assert (new["head_x"], new["head_y"], new["tail_x"], new["tail_y"]) == (
        *HEAD,
        *TAIL,
    )


async def test_a_learners_review_waits_and_changes_no_label(
    lab, people, frame, app_engine, owner_engine
):
    recorded = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))

    assert (recorded.level, recorded.counts) == ("learning", False)
    assert [l["source"] for l in await _labels(owner_engine, frame[0])] == ["human"]


async def test_a_trusted_confirm_applies_the_learners_points_exactly(
    lab, people, frame, app_engine, owner_engine
):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))

    await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[TRUSTED],
                  kind="head_tail", action="confirm", confirms_review_id=first.review_id)  # fmt: skip

    *_, new = await _labels(owner_engine, frame[0])
    assert new["review_id"] == first.review_id
    assert (new["head_x"], new["head_y"], new["tail_x"], new["tail_y"]) == (
        *HEAD,
        *TAIL,
    )
    reviews = await _in_reviews(app_engine, lab, frame[0])
    assert reviews[first.review_id].counts


async def test_a_second_learner_agreeing_applies_the_first_learners_points(
    lab, people, frame, app_engine, owner_engine
):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))
    near = (HEAD[0] + AGREE_PX / 2, HEAD[1])

    await _review(app_engine, lab, **_head_tail(people, LEARNER_2, frame, head=near,
                                                confirms_review_id=first.review_id))  # fmt: skip

    *_, new = await _labels(owner_engine, frame[0])
    assert new["review_id"] == first.review_id
    assert (new["head_x"], new["head_y"]) == HEAD  # the first's, not an average


async def test_a_second_learner_who_disagrees_applies_nothing(
    lab, people, frame, app_engine, owner_engine
):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))
    far = (HEAD[0] + AGREE_PX * 3, HEAD[1])

    second = await _review(app_engine, lab, **_head_tail(
        people, LEARNER_2, frame, head=far, confirms_review_id=first.review_id))  # fmt: skip

    assert not second.counts
    assert [l["source"] for l in await _labels(owner_engine, frame[0])] == ["human"]


async def test_a_learner_cannot_confirm(lab, people, frame, app_engine):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))

    with pytest.raises(ReviewRefused, match="Trusted"):
        await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[LEARNER_2],
                      kind="head_tail", action="confirm", confirms_review_id=first.review_id)  # fmt: skip


async def test_nobody_backs_their_own_review(lab, people, frame, app_engine):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))

    with pytest.raises(ReviewRefused, match="own"):
        await _review(app_engine, lab, **_head_tail(people, LEARNER, frame,
                                                    confirms_review_id=first.review_id))  # fmt: skip


async def test_a_review_that_already_counts_needs_no_confirm(
    lab, people, frame, app_engine
):
    counted = await _review(app_engine, lab, **_head_tail(people, EXPERT, frame))

    with pytest.raises(ReviewRefused, match="counts"):
        await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[TRUSTED],
                      kind="head_tail", action="confirm", confirms_review_id=counted.review_id)  # fmt: skip


async def test_an_experts_dot_becomes_the_laser_label(
    lab, people, frame, app_engine, owner_engine
):
    c, render = frame

    await _review(app_engine, lab, capture_id=c, reviewer_id=people[EXPERT],
                  kind="laser", action="fix", point=(772.0, 521.0), render_id=render)  # fmt: skip

    old, new = await _labels(owner_engine, c, "laser_labels")
    assert (old["superseded"], old["superseded_reason"]) == (True, "review")
    assert (new["source"], new["x"], new["y"]) == ("review", 772.0, 521.0)


async def test_a_reject_is_a_verdict_and_touches_no_label(
    lab, people, frame, app_engine, owner_engine
):
    recorded = await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[EXPERT],
                             kind="head_tail", action="reject", reason="bad_mask")  # fmt: skip

    assert recorded.counts
    assert [l["source"] for l in await _labels(owner_engine, frame[0])] == ["human"]


async def test_a_species_review_is_recorded_only(lab, people, frame, app_engine):
    recorded = await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[EXPERT],
                             kind="species", action="fix", scientific_name="Scarus coeruleus")  # fmt: skip

    assert recorded.counts and recorded.label_id is None


async def test_points_on_a_render_under_an_old_camera_are_refused(
    lab, people, frame, app_engine, owner_engine
):
    c, old_render = frame
    await _render(owner_engine, lab, c, new_camera=True)  # recalibrated since

    with pytest.raises(ReviewRefused, match="camera"):
        await _review(app_engine, lab, **_head_tail(people, EXPERT, (c, old_render)))


async def test_skills_are_per_kind_and_the_latest_counts(lab, people, app_engine):
    async with tenant_transaction(app_engine, lab) as conn:
        await set_skill(
            conn, lab, people[LEARNER], "laser", "expert", set_by=people[ADMIN]
        )
        await set_skill(
            conn, lab, people[LEARNER], "laser", "trusted", set_by=people[ADMIN]
        )
        laser = await reviewer_level(conn, lab, people[LEARNER], "laser")
        species = await reviewer_level(conn, lab, people[LEARNER], "species")

    assert (laser, species) == ("trusted", "learning")


async def test_populate_does_not_revive_a_label_a_review_superseded(
    lab, people, frame, app_engine, owner_engine
):
    from fishsense_services_api.headtail_store import record_head_tail_task

    c, _ = frame
    await exec_(owner_engine, "UPDATE head_tail_labels SET ls_project_id = 7, "
                "ls_task_id = 70 WHERE capture_id = :c", c=c)  # fmt: skip
    await _review(app_engine, lab, **_head_tail(people, EXPERT, frame))

    async with tenant_transaction(app_engine, lab) as conn:
        await record_head_tail_task(conn, lab, c, ls_project_id=7, ls_task_id=71)

    old, new = await _labels(owner_engine, c)
    assert old["superseded"] and not new["superseded"]


async def _in_reviews(app_engine, lab, capture_id):
    async with tenant_transaction(app_engine, lab) as conn:
        return {r.review_id: r for r in await capture_reviews(conn, lab, capture_id)}


# -- code review of #293 ------------------------------------------------------------
#
# A reviewed frame's labels come from the app: Label Studio isn't asked for
# them again, nothing revives what a review superseded, and an older or
# weaker review can't undo one that counts.


async def _unlabelled_frame(lab, owner_engine, *, laser=True):
    """A frame with no head/tail label (and with ``laser``, a Label Studio dot),
    and its render."""
    mission_id, _ = await calibrated_dive(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    if laser:
        await laser_label(owner_engine, lab, c, x=770.0, y=524.0)
    await _render(owner_engine, lab, c)
    return c, await _render_of(owner_engine, c), mission_id


async def _render_of(owner_engine, c, *, newest=True):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT id FROM capture_renders WHERE capture_id = :c "
                    f"ORDER BY seq {'DESC' if newest else 'ASC'} LIMIT 1"
                ),
                {"c": c},
            )
        ).scalar_one()


async def test_a_reviewed_frame_is_not_sent_to_label_studio_again(
    lab, people, frame, app_engine
):
    """Stage 5.1 asked for a head/tail of every frame with a dot and no live
    Label Studio row. A review supersedes that row, so the frame was imported
    again every wake, and populate (which won't revive it) never ended it."""
    from fishsense_services_api.headtail_store import (
        next_mission_for_headtail_preprocessing,
    )

    await _review(app_engine, lab, **_head_tail(people, EXPERT, frame))

    async with tenant_transaction(app_engine, lab) as conn:
        assert await next_mission_for_headtail_preprocessing(conn, lab) is None


async def test_a_frame_reviewed_in_the_app_is_not_label_studio_work(
    lab, people, app_engine, owner_engine
):
    """Nor a frame that never had a Label Studio row: its labels come from the
    app, whatever its review's level."""
    from fishsense_services_api.headtail_store import headtail_preprocess_inputs

    c, render, mission_id = await _unlabelled_frame(lab, owner_engine)
    await _review(app_engine, lab, **_head_tail(people, LEARNER, (c, render)))

    async with tenant_transaction(app_engine, lab) as conn:
        found = await headtail_preprocess_inputs(conn, lab, mission_id)
    assert found.captures == []


async def test_a_frame_whose_dot_was_reviewed_needs_no_laser_jpeg(
    lab, people, app_engine, owner_engine
):
    from fishsense_services_api.laser_store import laser_preprocess_captures

    c, render, mission_id = await _unlabelled_frame(lab, owner_engine, laser=False)
    await _review(app_engine, lab, capture_id=c, reviewer_id=people[EXPERT],
                  kind="laser", action="fix", point=(772.0, 521.0), render_id=render)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await laser_preprocess_captures(conn, lab, mission_id) == []


async def test_a_remediation_does_not_revive_a_dot_a_review_superseded(
    lab, people, frame, app_engine, owner_engine
):
    from fishsense_services_api.laser_store import (
        laser_label_population,
        revive_laser_labels,
    )

    c, render = frame
    await _review(app_engine, lab, capture_id=c, reviewer_id=people[EXPERT],
                  kind="laser", action="fix", point=(772.0, 521.0), render_id=render)  # fmt: skip
    async with owner_engine.connect() as conn:
        mission_id, number = (
            await conn.execute(
                text(
                    "SELECT c.mission_id, l.number FROM laser_labels l JOIN captures c "
                    "ON c.id = l.capture_id WHERE l.capture_id = :c AND l.source = 'human'"
                ),
                {"c": c},
            )
        ).one()

    async with tenant_transaction(app_engine, lab) as conn:
        population = await laser_label_population(conn, lab, mission_id)
        revived = await revive_laser_labels(
            conn, lab, mission_id, [number], population.fingerprint
        )

    assert revived == 0
    old, new = await _labels(owner_engine, c, "laser_labels")
    assert old["superseded"] and not new["superseded"]


async def test_an_older_learners_review_cannot_undo_a_newer_one_that_counts(
    lab, people, frame, app_engine, owner_engine
):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))
    expert_head = (HEAD[0] + 100, HEAD[1])
    await _review(
        app_engine, lab, **_head_tail(people, EXPERT, frame, head=expert_head)
    )

    with pytest.raises(ReviewRefused, match="newer"):
        await _review(app_engine, lab, **_head_tail(
            people, LEARNER_2, frame, confirms_review_id=first.review_id))  # fmt: skip
    with pytest.raises(ReviewRefused, match="newer"):
        await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[TRUSTED],
                      kind="head_tail", action="confirm", confirms_review_id=first.review_id)  # fmt: skip

    *_, live = await _labels(owner_engine, frame[0])
    assert (live["head_x"], live["superseded"]) == (expert_head[0], False)


async def test_a_confirm_of_points_on_an_older_camera_is_refused(
    lab, people, frame, app_engine, owner_engine
):
    first = await _review(app_engine, lab, **_head_tail(people, LEARNER, frame))
    await _render(owner_engine, lab, frame[0], new_camera=True)  # recalibrated since

    with pytest.raises(ReviewRefused, match="camera"):
        await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[TRUSTED],
                      kind="head_tail", action="confirm", confirms_review_id=first.review_id)  # fmt: skip
    new_render = await _render_of(owner_engine, frame[0])
    with pytest.raises(ReviewRefused, match="camera"):
        await _review(app_engine, lab, **_head_tail(
            people, LEARNER_2, (frame[0], new_render), confirms_review_id=first.review_id))  # fmt: skip


async def test_rejecting_an_accepted_result_supersedes_the_label_it_made(
    lab, people, frame, app_engine, owner_engine
):
    """An accept made the automatic result the label; a counting reject of
    that result (any reason) takes it back. Nothing is left to measure."""
    await _review(
        app_engine, lab, **_head_tail(people, LEARNER, frame, action="accept")
    )
    accepted = await _review(app_engine, lab, **_head_tail(people, EXPERT, frame, action="accept"))  # fmt: skip

    await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[TRUSTED],
                  kind="head_tail", action="reject", reason="bad_mask")  # fmt: skip

    labels = await _labels(owner_engine, frame[0])
    assert not [l for l in labels if not l["superseded"]]
    assert {l["review_id"] for l in labels if l["source"] == "review"} == {
        accepted.review_id
    }


async def test_a_reject_of_the_result_alone_keeps_a_label_studio_label(
    lab, people, frame, app_engine, owner_engine
):
    """A wrong mask or fish, or a wrong dot, says the automatic result is
    wrong: it says nothing of a labeler's points."""
    for reason in ("bad_mask", "wrong_fish"):
        await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[EXPERT],
                      kind="head_tail", action="reject", reason=reason)  # fmt: skip

    assert [l["superseded"] for l in await _labels(owner_engine, frame[0])] == [False]


async def test_a_frame_with_no_measurable_fish_has_no_live_label(
    lab, people, frame, app_engine, owner_engine
):
    """No fish, or a fish that can't be measured, is said of the frame: its
    head/tail labels are superseded, whoever placed them."""
    first = await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[LEARNER],
                          kind="head_tail", action="reject", reason="no_fish")  # fmt: skip
    assert [l["superseded"] for l in await _labels(owner_engine, frame[0])] == [False]

    await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[TRUSTED],
                  kind="head_tail", action="confirm", confirms_review_id=first.review_id)  # fmt: skip

    (label,) = await _labels(owner_engine, frame[0])
    assert (label["superseded"], label["superseded_reason"]) == (True, "review")


async def test_a_device_without_a_pinhole_camera_says_so(
    lab, people, frame, app_engine, owner_engine
):
    await exec_(
        owner_engine,
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
        "port_model, camera_matrix, distortion_coefficients) SELECT :t, m.device_id, "
        "'axial_refractive', 'flat', '[[3100,0,2048],[0,3100,1536],[0,0,1]]', "
        "'[0,0,0,0,0]' FROM captures c JOIN missions m ON m.id = c.mission_id "
        "WHERE c.id = :c",
        t=lab,
        c=frame[0],
    )

    with pytest.raises(ReviewRefused, match="no current pinhole camera"):
        await _review(app_engine, lab, **_head_tail(people, EXPERT, frame))


async def test_a_review_of_no_such_frame_is_refused(lab, people, app_engine):
    from fishsense_services_api.review_store import FrameNotFound

    with pytest.raises(FrameNotFound):
        await _review(app_engine, lab, capture_id=__import__("uuid").uuid4(),
                      reviewer_id=people[EXPERT], kind="species", action="fix",
                      scientific_name="Scarus coeruleus")  # fmt: skip


async def test_a_review_names_its_own_frames_prediction(
    lab, people, frame, app_engine, owner_engine
):
    """A prediction of another frame isn't this frame's."""
    c, *_ = await _unlabelled_frame(lab, owner_engine)
    async with owner_engine.begin() as conn:
        prediction = (
            await conn.execute(
                text(
                    "INSERT INTO automatic_head_tail_predictions (tenant_id, "
                    "capture_id, status, predictor_version) VALUES "
                    "(:t, :c, 'no_detections', 1) RETURNING id"
                ),
                {"t": lab, "c": c},
            )
        ).scalar_one()

    with pytest.raises(ReviewRefused, match="prediction"):
        await _review(app_engine, lab, capture_id=frame[0], reviewer_id=people[EXPERT],
                      kind="species", action="fix", scientific_name="Scarus coeruleus",
                      automatic_head_tail_prediction_id=prediction)  # fmt: skip


async def test_only_a_member_of_the_tenant_has_a_skill_in_it(
    lab, people, app_engine, owner_engine
):
    from fishsense_services_api.review_store import NotAMember

    async with owner_engine.begin() as conn:
        outsider = (
            await conn.execute(
                text("INSERT INTO users (sub) VALUES ('outsider') RETURNING id")
            )
        ).scalar_one()

    with pytest.raises(NotAMember):
        async with tenant_transaction(app_engine, lab) as conn:
            await set_skill(
                conn, lab, outsider, "laser", "expert", set_by=people[ADMIN]
            )


async def test_the_status_agrees_a_reviewed_frame_is_not_label_studio_work(
    lab, people, app_engine, owner_engine
):
    """`dive_pipeline_status` reads the cohorts' terms (0063)."""
    c, render, mission_id = await _unlabelled_frame(lab, owner_engine, laser=False)
    await _review(app_engine, lab, capture_id=c, reviewer_id=people[EXPERT],
                  kind="laser", action="fix", point=(772.0, 521.0), render_id=render)  # fmt: skip
    assert (
        await _status(app_engine, lab, mission_id, "laser_preprocess_pending") is False
    )
    assert (
        await _status(app_engine, lab, mission_id, "headtail_preprocess_pending")
        is True
    )

    await _review(app_engine, lab, **_head_tail(people, LEARNER, (c, render)))

    assert (
        await _status(app_engine, lab, mission_id, "headtail_preprocess_pending")
        is False
    )


async def _status(app_engine, lab, mission_id, column):
    async with tenant_transaction(app_engine, lab) as conn:
        return (
            await conn.execute(
                text(
                    f"SELECT s.{column} FROM dive_pipeline_status s "
                    "JOIN missions m ON m.number = s.dive_id WHERE m.id = :m"
                ),
                {"m": mission_id},
            )
        ).scalar_one()
