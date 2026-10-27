"""The automatic slate-pose calibration (0078; the owner, 2026-10-26).

Calibration v4 fits the beam through dots on automatically posed slates. A
mission keeps its slate-pose calibration beside its size-constancy one (one
current per method). Its lengths use, of the accepted ones: its own slate
pose, its own size constancy, its link's slate pose, its link's size
constancy. The slate pose is owed only with the switch on
(`slate_pose_version`; off, the cohort is as before), for a mission with a
slate template, and again when new frames or a different template make it
stale. Pinned here:

* one current calibration per method;
* the precedence, case by case;
* a slate-pose row names its template;
* the work: off by default, on only for a mission with a template; done at
  the current version with the mission's template, owed again on a new frame
  or another template; and a slate pose doesn't stand in for size constancy.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from _slate_calibration_seed import slate_template
from depth_measure_seed import capture, exec_, forget_identities, tenant  # noqa: F401
from fishsense_services_api.automatic_results_store import (
    AutomaticHeadTailRow,
    next_mission_for_automatic_results,
    persist_automatic_calibration,
)
from test_automatic_results_store import (
    VERSIONS,
    _backlog_mission,
    _in,
    _label_free,
    _measure_inputs,
    _predict,
    _predicted,
)

pytestmark = pytest.mark.usefixtures("forget_identities")

POSE = "slate-pose-4"


def _posed(template, **extra):
    return _label_free(**{"method": "slate_pose", "slate_template_id": template,
                          "algorithm_version": POSE, **extra})  # fmt: skip


async def _with_template(owner_engine, mission_id):
    template = await slate_template(owner_engine)
    await exec_(owner_engine, "UPDATE missions SET slate_template_id = :s WHERE id = :m",
                s=template, m=mission_id)  # fmt: skip
    return template


async def _persist(app_engine, lab, mission_id, row):
    return await _in(app_engine, lab, persist_automatic_calibration, mission_id, row)


async def test_a_mission_keeps_one_current_calibration_per_method(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    constancy = await _persist(app_engine, lab, mission_id, _label_free())
    posed = await _persist(app_engine, lab, mission_id, _posed(template))

    async with owner_engine.connect() as conn:
        current = dict((await conn.execute(text(
            "SELECT method, id FROM current_automatic_laser_calibrations "
            "WHERE mission_id = :m"), {"m": mission_id})).all())  # fmt: skip

    assert current == {"size_constancy": constancy, "slate_pose": posed}


async def test_a_slate_pose_names_its_template(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)

    with pytest.raises(IntegrityError, match="slate_template_check"):
        await _persist(app_engine, lab, mission_id, _posed(None))


async def _measured_with(app_engine, lab, mission_id, owner_engine):
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    return (await _measure_inputs(app_engine, lab, mission_id)).calibration


async def test_lengths_take_the_slate_pose_first_then_size_constancy_then_the_link(
    owner_engine, app_engine
):
    """The owner (2026-10-26): own slate pose, own size constancy, the link's
    slate pose, the link's size constancy."""
    lab = await tenant(owner_engine)
    session = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, session)
    link_constancy = await _persist(app_engine, lab, session, _label_free())
    mission_id = await _backlog_mission(owner_engine, lab, source_mission=session)

    chosen = lambda: _measured_with(
        app_engine, lab, mission_id, owner_engine
    )  # noqa: E731
    assert (await chosen()).automatic_laser_calibration_id == link_constancy
    link_pose = await _persist(app_engine, lab, session, _posed(template))
    assert (await chosen()).automatic_laser_calibration_id == link_pose
    own_constancy = await _persist(app_engine, lab, mission_id, _label_free())
    assert (await chosen()).automatic_laser_calibration_id == own_constancy
    own_pose = await _persist(app_engine, lab, mission_id, _posed(template))
    assert (await chosen()).automatic_laser_calibration_id == own_pose
    # A refused slate pose leaves the next in line.
    await _persist(app_engine, lab, mission_id, _posed(
        template, outcome="refused", refusal_reason="too_few_frames",
        laser_position=None, laser_axis=None))  # fmt: skip
    assert (await chosen()).automatic_laser_calibration_id == own_constancy


async def _next(app_engine, lab, *, pose=None):
    candidate = await _in(app_engine, lab, next_mission_for_automatic_results,
                          **VERSIONS, species=False, slate_pose_version=pose)  # fmt: skip
    return None if candidate is None else candidate.mission_id


async def _calibrated(owner_engine, app_engine, lab):
    """A mission whose frames and size-constancy fit are done: nothing owed."""
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=c, status="slate_frame", laser_x=1.0,
                                        laser_y=2.0, predictor_version=1))  # fmt: skip
    await _persist(app_engine, lab, mission_id,
                   _label_free(outcome="refused", refusal_reason="too_few_frames",
                               laser_position=None, laser_axis=None))  # fmt: skip
    return mission_id


async def test_the_slate_pose_is_owed_only_with_the_switch_on_and_a_template(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _calibrated(owner_engine, app_engine, lab)
    assert await _next(app_engine, lab) is None
    assert await _next(app_engine, lab, pose=POSE) is None  # no template

    template = await _with_template(owner_engine, mission_id)
    assert await _next(app_engine, lab) is None  # the switch is off
    assert await _next(app_engine, lab, pose=POSE) == mission_id

    await _persist(app_engine, lab, mission_id, _posed(template, outcome="refused",
                   refusal_reason="too_few_frames", laser_position=None,
                   laser_axis=None))  # fmt: skip
    assert await _next(app_engine, lab, pose=POSE) is None
    assert await _next(app_engine, lab, pose="slate-pose-5") == mission_id


async def test_another_template_or_a_new_frame_owes_it_again(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _calibrated(owner_engine, app_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    await _persist(app_engine, lab, mission_id, _posed(template))
    assert await _next(app_engine, lab, pose=POSE) is None

    await _with_template(owner_engine, mission_id)  # another template
    assert await _next(app_engine, lab, pose=POSE) == mission_id
    await exec_(owner_engine, "UPDATE missions SET slate_template_id = :s WHERE id = :m",
                s=template, m=mission_id)  # fmt: skip
    assert await _next(app_engine, lab, pose=POSE) is None

    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=c, status="slate_frame", laser_x=3.0,
                                        laser_y=4.0, predictor_version=1))  # fmt: skip
    # Both are stale now; once size constancy is refitted, the pose still is.
    await _persist(app_engine, lab, mission_id,
                   _label_free(outcome="refused", refusal_reason="too_few_frames",
                               laser_position=None, laser_axis=None))  # fmt: skip
    assert await _next(app_engine, lab) is None
    assert await _next(app_engine, lab, pose=POSE) == mission_id


async def test_a_slate_pose_does_not_stand_in_for_size_constancy(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id,
                   AutomaticHeadTailRow(capture_id=c, status="slate_frame", laser_x=1.0,
                                        laser_y=2.0, predictor_version=1))  # fmt: skip
    # Even at size constancy's own version string: the method decides.
    await _persist(app_engine, lab, mission_id, _posed(
        template, algorithm_version=VERSIONS["calibration_version"]))  # fmt: skip

    assert await _next(app_engine, lab) == mission_id  # size constancy owed


async def test_a_missions_review_shows_the_calibration_its_lengths_use(
    owner_engine, app_engine
):
    """Two current calibrations (0078): the review names the accepted one in
    the lengths' order, and its method."""
    from fishsense_services_api.automatic_review_store import mission_automatic

    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    await _persist(app_engine, lab, mission_id, _label_free(
        outcome="refused", refusal_reason="too_few_frames", laser_position=None,
        laser_axis=None))  # fmt: skip
    await _persist(app_engine, lab, mission_id, _posed(template))

    run = await _in(app_engine, lab, mission_automatic, mission_id)

    assert (run.calibration.method, run.calibration.outcome) == ("slate_pose",
                                                                 "accepted")  # fmt: skip


async def test_whether_one_missions_slate_pose_is_owed(owner_engine, app_engine):
    """What the chain's calibration step asks before posing (the cohort's
    term, for the one mission)."""
    from fishsense_services_api.automatic_results_store import automatic_slate_pose_owed

    lab = await tenant(owner_engine)
    mission_id = await _calibrated(owner_engine, app_engine, lab)
    owed = lambda: _in(app_engine, lab, automatic_slate_pose_owed, mission_id,  # noqa: E731
                       version=POSE)  # fmt: skip
    assert await owed() is False  # no template

    template = await _with_template(owner_engine, mission_id)
    assert await owed() is True
    await _persist(app_engine, lab, mission_id, _posed(template))
    assert await owed() is False
