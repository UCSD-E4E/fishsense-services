"""The laser-color backfill's database side (0076; the owner, 2026-10-26).

Automatic dots predicted before contract v22 kept no color. The backfill
reads each one's color off its render and records it in
``automatic_laser_colour_readings``: append-only, naming its source and
version, never on the prediction row. A page of readings is compared with the
labelers' colors and the live kernel's on the same frames; only with
``apply`` are readings written, and only for a dot still current with no
color of its own. Reads use the dot's own color first, then the reading.
"""

from __future__ import annotations

import uuid

import pytest
from depth_measure_seed import (
    exec_,
    forget_identities,
    laser_label,
    tenant,
)  # noqa: F401
from sqlalchemy import text
from test_automatic_results_store import _in, _measured, _predict, _predicted

from fishsense_services_api.laser_colour_backfill_store import (
    ColourReadingRow,
    colour_backfill_frames,
    colour_backfill_missions,
    record_colour_page,
)


async def _label(owner_engine, lab, c, label):
    labelled = await laser_label(owner_engine, lab, c)
    await exec_(owner_engine, "UPDATE laser_labels SET label = :l WHERE id = :i",
                l=label, i=labelled)  # fmt: skip


async def _readings(owner_engine):
    async with owner_engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT capture_id, color, color_margin, source, reading_version "
            "FROM automatic_laser_colour_readings ORDER BY seq"))  # fmt: skip
        return [tuple(r) for r in rows]


def _reading(frame, colour="red", margin=150.0, status="read"):
    return ColourReadingRow(prediction_id=frame.prediction_id,
                            capture_id=frame.capture_id, status=status,
                            color=colour, margin=margin)  # fmt: skip


async def test_the_work_is_every_current_dot_with_a_render(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    _, kernel = await _measured(owner_engine, app_engine, lab, laser_color="green",
                                laser_color_margin=-80.0)  # fmt: skip

    missions = await _in(app_engine, lab, colour_backfill_missions)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    assert sorted(m.frames for m in missions) == [1, 1]
    assert (frame.capture_id, frame.x, frame.y, frame.kernel_color) == (
        c, 2000.0, 1500.0, None)  # fmt: skip
    assert frame.render_version == 1 and frame.camera_calibration_id is not None


async def test_a_dry_run_compares_and_writes_nothing(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _label(owner_engine, lab, c, "Red Laser")
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame, "red")], apply=False)  # fmt: skip

    assert counts["read"] == {"red": 1}
    assert counts["vs_label"] == {"red/red": 1}
    assert counts["written"] == 0
    assert await _readings(owner_engine) == []


async def test_disagreement_and_the_live_kernel_are_counted(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab, laser_color="green",
                                    laser_color_margin=-80.0)  # fmt: skip
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)
    await _label(owner_engine, lab, frame.capture_id, "Green Laser")

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame, "red")], apply=False)  # fmt: skip

    assert counts["vs_label"] == {"red/green": 1}
    assert counts["vs_kernel"] == {"red/green": 1}


async def test_apply_writes_a_reading_for_an_uncolored_dot(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame, "red", 163.5)], apply=True)  # fmt: skip

    assert counts["written"] == 1
    assert await _readings(owner_engine) == [(c, "red", 163.5, "render", 1)]


async def test_apply_never_overrides_the_dots_own_color(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab, laser_color="green",
                                    laser_color_margin=-80.0)  # fmt: skip
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame, "red")], apply=True)  # fmt: skip

    assert counts["written"] == 0
    assert await _readings(owner_engine) == []


async def test_apply_skips_a_dot_no_longer_current(owner_engine, app_engine):
    """A newer prediction since the page was read: its reading is stale."""
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c, head_x=1700.0))

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame)], apply=True)  # fmt: skip

    assert counts["written"] == 0


async def test_apply_twice_writes_once(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    for _ in range(2):
        await _in(app_engine, lab, record_colour_page, mission_id,
                  [_reading(frame)], apply=True)  # fmt: skip

    assert len(await _readings(owner_engine)) == 1


@pytest.mark.parametrize("status", ["render_unavailable", "decode_failed", "failed"])
async def test_only_a_reading_is_written(owner_engine, app_engine, status):
    """A frame with no reading stays owed, for a later run."""
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame, None, None, status)], apply=True)  # fmt: skip

    assert counts["written"] == 0 and counts["status"] == {status: 1}


async def test_too_close_to_call_is_written_with_no_color(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)

    counts = await _in(app_engine, lab, record_colour_page, mission_id,
                       [_reading(frame, None, 2.0)], apply=True)  # fmt: skip

    assert counts["read"] == {"too_close": 1}
    assert await _readings(owner_engine) == [(c, None, 2.0, "render", 1)]


async def test_a_reading_of_another_missions_dot_is_refused(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    other, _ = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, other)

    with pytest.raises(ValueError, match="not the mission's"):
        await _in(app_engine, lab, record_colour_page, mission_id,
                  [_reading(frame)], apply=True)  # fmt: skip


# -- the reads: the dot's own color, else the reading, saying which ----------------

from test_automatic_review_api import _lab, _number  # noqa: E402
from test_observations_api import _get, client  # noqa: E402,F401


async def _backfilled(owner_engine, app_engine, lab, colour="red", margin=150.0):
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    (frame,) = await _in(app_engine, lab, colour_backfill_frames, mission_id)
    await _in(app_engine, lab, record_colour_page, mission_id,
              [_reading(frame, colour, margin)], apply=True)  # fmt: skip
    return mission_id, c


async def test_a_frame_shows_a_backfilled_color_and_where_it_came_from(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, c = await _backfilled(owner_engine, app_engine, lab, "red", 163.5)

    body = (await _get(client, f"/tenants/lab/captures/{c}/automatic")).json()

    assert (body["laser_color"], body["laser_color_margin"]) == ("red", 163.5)
    assert body["laser_color_source"] == "render"


async def test_the_dots_own_color_is_shown_as_the_dots(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, c = await _measured(owner_engine, app_engine, lab, laser_color="green",
                           laser_color_margin=-80.0)  # fmt: skip

    body = (await _get(client, f"/tenants/lab/captures/{c}/automatic")).json()

    assert (body["laser_color"], body["laser_color_source"]) == ("green", "dot")


async def test_a_dot_with_no_color_says_no_source(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, c = await _measured(owner_engine, app_engine, lab)

    body = (await _get(client, f"/tenants/lab/captures/{c}/automatic")).json()

    assert (body["laser_color"], body["laser_color_source"]) == (None, None)


async def test_each_length_and_the_missions_counts_use_the_reading(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    mission_id, _ = await _backfilled(owner_engine, app_engine, lab, "green", -90.0)
    number = await _number(owner_engine, mission_id)

    (row,) = (
        await _get(client, f"/tenants/lab/missions/{number}/automatic/lengths")
    ).json()["lengths"]
    mission = (await _get(client, f"/tenants/lab/missions/{number}")).json()

    assert (row["laser_color"], row["laser_color_source"]) == ("green", "render")
    assert mission["laser_colors"]["automatic"] == {"red": 0, "green": 1, "unread": 0}
