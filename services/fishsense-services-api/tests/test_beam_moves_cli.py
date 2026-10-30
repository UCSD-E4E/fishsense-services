"""Recording the moves, and withdrawing the lengths after them (the owner,
2026-10-25: a partial run -- the part of the dive before the move).

``detect-beam-moves`` runs the detector over every mission's wide automatic
dots and, with ``--apply``, records each move it finds that is not already
the mission's current one. ``withdraw-moved-lengths`` appends a
``beam_moved`` refusal after each current automatic length on a frame inside
a recorded move's window (append-only: the length stays in the history).
Since each aim is calibrated (0081), a length after the move measured with
the first aim's beam is no longer current, so it has nothing to withdraw.
Both are dry runs unless told otherwise, as the owner. Pinned here:

* a mission whose dots switch to a parallel line is listed with its window;
  one on one line is not; a zoomed frame's dot is not read;
* the apply records exactly the listed moves, and a re-run lists nothing new;
* a length after a move is not current, so the withdrawal finds none.
"""

import numpy as np
import pytest
from sqlalchemy import text

from _slate_calibration_seed import zoom
from depth_measure_seed import capture, forget_identities, tenant  # noqa: F401
from fishsense_services_api.automatic_results_store import AutomaticHeadTailRow
from fishsense_services_api.cli import main
from test_automatic_results_store import _backlog_mission, _measured, _predict

pytestmark = pytest.mark.usefixtures("forget_identities")


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


async def _dots(owner_engine, app_engine, lab, mission_id, n, *, offset=0.0, seed=0):
    """`n` frames, each later than the last, their dots along one line
    (72 deg), shifted sideways by `offset` px."""
    rng = np.random.default_rng(seed)
    a = np.radians(72.0)
    u, normal = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
    frames = []
    for s in rng.uniform(-300, 300, n):
        x, y = (
            np.array([2000.0, 1500.0]) + s * u + (offset + rng.normal(0, 1.5)) * normal
        )
        c = await capture(owner_engine, lab, mission_id)
        await _predict(app_engine, lab, mission_id, AutomaticHeadTailRow(
            capture_id=c, status="slate_frame", laser_x=float(x), laser_y=float(y),
            slate_probability=0.9, predictor_version=1), rendered=False)  # fmt: skip
        frames.append(c)
    return frames


async def _moves(owner_engine):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT mission_id, moved_after, moved_before FROM mission_beam_moves"
                )
            )
        ).all()


async def test_detection_is_a_dry_run_then_records_the_moves(
    owner_engine, app_engine, as_owner, capsys
):
    lab = await tenant(owner_engine)
    moved = await _backlog_mission(owner_engine, lab)
    first = await _dots(owner_engine, app_engine, lab, moved, 20)
    second = await _dots(owner_engine, app_engine, lab, moved, 20, offset=45.0, seed=1)
    steady = await _backlog_mission(owner_engine, lab)
    await _dots(owner_engine, app_engine, lab, steady, 40)

    assert await main(["detect-beam-moves"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "1 move" in out
    assert await _moves(owner_engine) == []

    assert await main(["detect-beam-moves", "--apply"]) == 0
    async with owner_engine.connect() as conn:
        at = dict((await conn.execute(text(
            "SELECT id, captured_at FROM captures WHERE id = ANY(:c)"),
            {"c": [first[-1], second[0]]})).all())  # fmt: skip
    assert [(m.mission_id, m.moved_after, m.moved_before)
            for m in await _moves(owner_engine)] == [
        (moved, at[first[-1]], at[second[0]])]  # fmt: skip

    capsys.readouterr()
    assert await main(["detect-beam-moves"]) == 0
    assert "no new move" in capsys.readouterr().out


async def test_a_zoomed_frames_dot_is_not_read(owner_engine, app_engine, as_owner,
                                               capsys):  # fmt: skip
    """Its dot sits on another line for another reason (0070)."""
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    await _dots(owner_engine, app_engine, lab, mission_id, 20)
    for c in await _dots(owner_engine, app_engine, lab, mission_id, 20, offset=45.0,
                         seed=1):  # fmt: skip
        await zoom(owner_engine, c, 5.4)

    assert await main(["detect-beam-moves"]) == 0
    assert "no new move" in capsys.readouterr().out


async def test_a_length_after_a_move_is_not_current_so_none_is_withdrawn(
    owner_engine, app_engine, as_owner, capsys
):
    from _slate_calibration_seed import beam_moved_after

    lab = await tenant(owner_engine)
    _, before = await _measured(owner_engine, app_engine, lab)
    mission_id, after = await _measured(owner_engine, app_engine, lab)
    earlier = await capture(owner_engine, lab, mission_id)
    async with owner_engine.begin() as conn:
        await conn.execute(text(
            "UPDATE captures SET captured_at = captured_at - interval '1 hour' "
            "WHERE id = :c"), {"c": earlier})  # fmt: skip
    await beam_moved_after(owner_engine, earlier)

    assert await main(["withdraw-moved-lengths"]) == 0
    assert "Would withdraw" not in capsys.readouterr().out
    async with owner_engine.connect() as conn:
        current = dict(
            (
                await conn.execute(
                    text(
                        "SELECT capture_id, refusal FROM current_automatic_measurements"
                    )
                )
            ).all()
        )
    assert after not in current and current[before] is None
