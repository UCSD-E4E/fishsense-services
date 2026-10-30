"""A calibration per laser aim (0081; the owner, 2026-10-29).

0073's partial run calibrated and measured a mission only before a recorded
beam move and refused the rest: 37 missions, ~4,300 frames after their move.
Now each aim is its own calibration: ``beam_aim()`` names a frame's aim (0
before the move, or with none; 1 after; NULL inside the window, which
belongs to neither), a calibration row carries its aim, a frame is fitted and
measured only with its own aim's. Pinned here:

* `beam_aim` at each side of the window;
* one current calibration per method and aim; the inputs, the work, the
  measure inputs and the current lengths, each by aim; aim 1 has no link;
* a row naming a frame of another aim is refused;
* a length inside a move's window is neither aim's, never current: so
  `withdraw-moved-lengths` finds none to withdraw.
"""

from __future__ import annotations


import numpy as np
import pytest
from sqlalchemy import text

from _slate_calibration_seed import beam_moved_after
from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    K,
    capture,
    device,
    exec_,
    forget_identities,
    mission,
    tenant,
)
from fishsense_services_api.automatic_results_store import (
    AutomaticMeasurementRow,
    InvalidAutomaticResults,
    automatic_calibration_inputs,
    automatic_measure_inputs,
    persist_automatic_calibration,
    persist_automatic_measurements,
)
from fishsense_services_api.zoom_gate import zoomed_automatic_lengths
from test_automatic_results_store import (
    _backlog_mission,
    _in,
    _label_free,
    _next_without_species as _next,
    _predict,
    _predicted,
    _rows,
)

pytestmark = pytest.mark.usefixtures("forget_identities")

#: A beam from the housing (`_label_free`'s), and one a degree off it.
BEAM = ([0.03, -0.1, 0.0], [0.01, 0.03, 0.9995])
OTHER_BEAM = ([0.03, -0.1, 0.0], [0.03, 0.045, 0.9985])


def _dots(beam, n, *, shift_px=0.0, noise=1.0, seed=0):
    """`n` dots `beam` casts at 0.6-4 m through `K`, moved `shift_px`
    sideways off its image line."""
    rng = np.random.default_rng(seed)
    k = np.asarray(K)
    o, a = (np.asarray(v, float) for v in beam)
    a = a / np.linalg.norm(a)
    pixels = []
    for z in rng.uniform(0.6, 4.0, n):
        p = k @ (o + ((z - o[2]) / a[2]) * a)
        pixels.append(p[:2] / p[2])
    pixels = np.array(pixels)
    ends = [k @ (o + ((z - o[2]) / a[2]) * a) for z in (1.0, 3.0)]
    u = ends[1][:2] / ends[1][2] - ends[0][:2] / ends[0][2]
    normal = np.array([-u[1], u[0]]) / np.linalg.norm(u)
    return pixels + (shift_px + rng.normal(0, noise, (n, 1))) * normal


async def _frames(owner_engine, app_engine, lab, mission_id, dots):
    """A predicted fish per dot, in time order."""
    frames = [await capture(owner_engine, lab, mission_id) for _ in dots]
    await _predict(app_engine, lab, mission_id, *[
        _predicted(c, laser_x=float(x), laser_y=float(y))
        for c, (x, y) in zip(frames, dots)
    ])  # fmt: skip
    return frames


async def _aim(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT beam_aim(tenant_id, mission_id, captured_at) FROM captures "
            "WHERE id = :c"), {"c": capture_id})).scalar_one()  # fmt: skip


async def _persist(app_engine, lab, mission_id, **row):
    return await _in(app_engine, lab, persist_automatic_calibration, mission_id,
                     _label_free(**row))  # fmt: skip


async def _measure(app_engine, lab, mission_id, aim=0):
    return await _in(app_engine, lab, automatic_measure_inputs, mission_id,
                     algorithm_version="1", aim=aim)  # fmt: skip


async def _moved(owner_engine, app_engine, lab, *, before=2, after=2):
    """A mission whose laser moved between its first `before` frames and its
    last `after`: ``(mission, before frames, after frames)``."""
    mission_id = await _backlog_mission(owner_engine, lab)
    first = await _frames(owner_engine, app_engine, lab, mission_id,
                          _dots(BEAM, before))  # fmt: skip
    second = await _frames(owner_engine, app_engine, lab, mission_id,
                           _dots(OTHER_BEAM, after, seed=1))  # fmt: skip
    await beam_moved_after(owner_engine, first[-1])
    return mission_id, first, second


# -- the aim --------------------------------------------------------------------------


async def test_a_frames_aim_is_0_before_the_move_1_after_and_none_inside(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    still = await capture(owner_engine, lab, mission_id)
    assert await _aim(owner_engine, still) == 0  # no move
    last, inside, after = [await capture(owner_engine, lab, mission_id)
                           for _ in range(3)]  # fmt: skip
    await exec_(owner_engine, """
        INSERT INTO mission_beam_moves (tenant_id, mission_id, moved_after,
                                        moved_before, detector_version)
        SELECT tenant_id, mission_id, captured_at,
               (SELECT captured_at FROM captures WHERE id = :after), 'test'
        FROM captures WHERE id = :last
        """, last=last, after=after)  # fmt: skip

    assert [await _aim(owner_engine, c) for c in (still, last, inside, after)] == [
        0, 0, None, 1,
    ]  # fmt: skip


async def test_a_calibration_is_current_per_method_and_aim(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _, _ = await _moved(owner_engine, app_engine, lab)
    first = await _persist(app_engine, lab, mission_id)
    second = await _persist(app_engine, lab, mission_id, aim=1)

    current = await _rows(owner_engine, """
        SELECT a.id, x.aim FROM current_automatic_laser_calibrations a
        JOIN automatic_laser_calibrations x ON x.id = a.id
        WHERE a.mission_id = :m ORDER BY x.aim""", m=mission_id)  # fmt: skip

    assert current == [{"id": first, "aim": 0}, {"id": second, "aim": 1}]


async def test_each_aims_fit_reads_only_its_own_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, first, second = await _moved(owner_engine, app_engine, lab)

    async def detector(conn, tenant_id, d):
        return [(f, 0.9) for f in (*first, *second)]

    inputs = {
        aim: await _in(app_engine, lab, automatic_calibration_inputs, mission_id,
                       slate_frames=detector, aim=aim)  # fmt: skip
        for aim in (0, 1)
    }

    assert [c.capture_id for c in inputs[0].candidates] == first
    assert [c.capture_id for c in inputs[1].candidates] == second
    assert len(inputs[1].line_dots) == len(second)


async def test_a_moved_mission_owes_its_second_aims_fit(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _, _ = await _moved(owner_engine, app_engine, lab)
    await _persist(app_engine, lab, mission_id, outcome="refused",
                   refusal_reason="too_few_frames", laser_position=None,
                   laser_axis=None)  # fmt: skip
    assert await _next(app_engine, lab) == mission_id  # aim 1 owes one

    await _persist(app_engine, lab, mission_id, aim=1, outcome="refused",
                   refusal_reason="too_few_frames", laser_position=None,
                   laser_axis=None)  # fmt: skip
    assert await _next(app_engine, lab) is None


async def test_a_fit_naming_a_frame_of_the_other_aim_is_refused(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, first, _ = await _moved(owner_engine, app_engine, lab)

    with pytest.raises(InvalidAutomaticResults, match="another aim"):
        await _persist(app_engine, lab, mission_id, aim=1, capture_ids=[first[0]])


async def test_each_aims_frames_are_measured_with_its_own_calibration(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, first, second = await _moved(owner_engine, app_engine, lab)
    own = await _persist(app_engine, lab, mission_id)
    assert (await _measure(app_engine, lab, mission_id, aim=1)).calibration is None

    moved = await _persist(app_engine, lab, mission_id, aim=1,
                           laser_axis=OTHER_BEAM[1])  # fmt: skip
    inputs = {aim: await _measure(app_engine, lab, mission_id, aim) for aim in (0, 1)}

    assert inputs[0].calibration.automatic_laser_calibration_id == own
    assert [c.capture_id for c in inputs[0].captures] == first
    assert inputs[1].calibration.automatic_laser_calibration_id == moved
    assert inputs[1].calibration.laser_axis == OTHER_BEAM[1]
    assert [c.capture_id for c in inputs[1].captures] == second


async def test_the_second_aim_never_takes_the_link(owner_engine, app_engine):
    """A link names the session that calibrates the mission's laser as it was
    set: the first aim. After a move it says nothing."""
    lab = await tenant(owner_engine)
    session = await _backlog_mission(owner_engine, lab)
    linked = await _persist(app_engine, lab, session)
    device_id = await device(owner_engine, lab)
    mission_id = await mission(owner_engine, lab, device_id=device_id,
                               priority="low", source_mission=session)  # fmt: skip
    first = await _frames(owner_engine, app_engine, lab, mission_id, _dots(BEAM, 1))
    await _frames(owner_engine, app_engine, lab, mission_id, _dots(OTHER_BEAM, 1))
    await beam_moved_after(owner_engine, first[-1])

    assert (await _measure(app_engine, lab, mission_id, 0)).calibration.automatic_laser_calibration_id == linked  # fmt: skip
    assert (await _measure(app_engine, lab, mission_id, 1)).calibration is None


async def _length(app_engine, lab, mission_id, inputs, capture_id):
    c = next(c for c in inputs.captures if c.capture_id == capture_id)
    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=c.capture_id,
            automatic_head_tail_prediction_id=c.automatic_head_tail_prediction_id,
            automatic_keypoint_prediction_id=c.automatic_keypoint_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            laser_calibration_id=inputs.calibration.laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
            length_m=0.42, depth_m=2.1, algorithm="laser_depth_fronto_parallel",
            algorithm_version="1", core_version="4.1.0",
        )
    ])  # fmt: skip


async def _current_lengths(owner_engine, mission_id):
    return {r["capture_id"] for r in await _rows(owner_engine, """
        SELECT m.capture_id FROM current_automatic_measurements m
        JOIN captures c ON c.id = m.capture_id WHERE c.mission_id = :m""",
        m=mission_id)}  # fmt: skip


async def test_a_length_is_current_only_under_its_frames_own_aim(
    owner_engine, app_engine
):
    """A frame measured before its mission's move was recorded keeps a
    length from the first aim's beam: it is not current once the move says
    the frame was on the second."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    first, later = await _frames(owner_engine, app_engine, lab, mission_id,
                                 _dots(BEAM, 2))  # fmt: skip
    await _persist(app_engine, lab, mission_id)
    inputs = await _measure(app_engine, lab, mission_id)
    for c in (first, later):
        await _length(app_engine, lab, mission_id, inputs, c)
    assert await _current_lengths(owner_engine, mission_id) == {first, later}

    await beam_moved_after(owner_engine, first)
    assert await _current_lengths(owner_engine, mission_id) == {first}

    await _persist(app_engine, lab, mission_id, aim=1)
    assert await _next(app_engine, lab) == mission_id  # `later` owes a length
    second = await _measure(app_engine, lab, mission_id, aim=1)
    assert [c.capture_id for c in second.captures] == [later]
    await _length(app_engine, lab, mission_id, second, later)
    assert await _current_lengths(owner_engine, mission_id) == {first, later}
    assert await _next(app_engine, lab) is None


async def test_no_length_is_current_inside_a_window_so_none_is_withdrawn(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    last, inside, after = await _frames(owner_engine, app_engine, lab, mission_id,
                                        _dots(BEAM, 3))  # fmt: skip
    await _persist(app_engine, lab, mission_id)
    inputs = await _measure(app_engine, lab, mission_id)
    for c in (last, inside, after):
        await _length(app_engine, lab, mission_id, inputs, c)
    await _persist(app_engine, lab, mission_id, aim=1)
    await exec_(owner_engine, """
        INSERT INTO mission_beam_moves (tenant_id, mission_id, moved_after,
                                        moved_before, detector_version)
        SELECT tenant_id, mission_id, captured_at,
               (SELECT captured_at FROM captures WHERE id = :after), 'test'
        FROM captures WHERE id = :last
        """, last=last, after=after)  # fmt: skip
    # Measured with the first aim's beam: `after` is the second aim's, and
    # `inside` neither's. Neither is current, so none is left to withdraw.
    assert await _current_lengths(owner_engine, mission_id) == {last}

    async with owner_engine.connect() as conn:
        listed = await zoomed_automatic_lengths(conn, "beam_moved")

    assert [x.capture_id for x in listed if x.mission_id == mission_id] == []
