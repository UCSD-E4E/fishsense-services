"""The automatic dot's colour (contract v22, 2026-10-25).

The laser kernel always read the dot's colour off its pixels; the automatic
frames stage dropped it, so no automatic row had one. It is now kept on the
row (``laser_color``/``laser_color_margin``, 0072) and shown wherever the
automatic dot is: a frame's result, the lengths list, and a mission's colour
counts beside the human labels' and the human-path predictions'.
"""

from __future__ import annotations

import pytest
from depth_measure_seed import exec_, forget_identities, tenant  # noqa: F401
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_automatic_results_store import (
    _backlog_mission,
    _in,
    _measured,
    _predict,
    _predicted,
)

from depth_measure_seed import capture


async def _current(owner_engine, c):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT laser_color, laser_color_margin "
                    "FROM current_automatic_head_tail_predictions WHERE capture_id = :c"
                ),
                {"c": c},
            )
        ).one()


async def test_a_row_keeps_its_dots_colour(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)

    await _predict(
        app_engine, lab, mission_id,
        _predicted(c, laser_color="green", laser_color_margin=0.31),
    )  # fmt: skip

    assert tuple(await _current(owner_engine, c)) == ("green", 0.31)


async def test_a_row_from_before_has_no_colour(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)

    assert tuple(await _current(owner_engine, c)) == (None, None)


async def test_a_colour_is_red_or_green(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)

    with pytest.raises(IntegrityError, match="laser_color"):
        await exec_(
            owner_engine,
            "UPDATE automatic_head_tail_predictions SET laser_color = 'blue'",
        )


# -- shown wherever the automatic dot is -------------------------------------------

from depth_measure_seed import laser_label  # noqa: E402
from test_automatic_review_api import _lab, _number  # noqa: E402
from test_observations_api import _get, client  # noqa: E402,F401


async def test_a_frames_result_shows_its_dots_colour(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, c = await _measured(
        owner_engine, app_engine, lab, laser_color="green", laser_color_margin=0.31
    )

    body = (await _get(client, f"/tenants/lab/captures/{c}/automatic")).json()

    assert (body["laser_color"], body["laser_color_margin"]) == ("green", 0.31)


async def test_each_automatic_length_shows_its_dots_colour(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    await _measured(owner_engine, app_engine, lab, laser_color="red")

    (row,) = (await _get(client, "/tenants/lab/automatic/lengths")).json()["lengths"]

    assert row["laser_color"] == "red"


async def _label(owner_engine, lab, c, label, **kwargs):
    labelled = await laser_label(owner_engine, lab, c, **kwargs)
    await exec_(
        owner_engine, "UPDATE laser_labels SET label = :l WHERE id = :i",
        l=label, i=labelled,
    )  # fmt: skip


async def _prediction(owner_engine, lab, c, color, *, x=10.0):
    await exec_(
        owner_engine,
        "INSERT INTO laser_predictions (tenant_id, capture_id, x, y, confidence, "
        "predictor_version, color) VALUES (:t, :c, :x, :x, 0.9, 3, :col)",
        t=lab, c=c, x=x, col=color,
    )  # fmt: skip


async def test_a_mission_counts_its_laser_colours_by_source(
    client, seed_memberships, owner_engine, app_engine
):
    """Counts, not a verdict: a mission whose sources disagree shows it."""
    lab = await _lab(seed_memberships)
    mission_id, c = await _measured(owner_engine, app_engine, lab, laser_color="red")
    frames = [await capture(owner_engine, lab, mission_id) for _ in range(3)]
    await _label(owner_engine, lab, c, "Red Laser")
    await _label(owner_engine, lab, frames[0], "Red Laser")
    await _label(owner_engine, lab, frames[1], "Green Laser")
    await _label(owner_engine, lab, frames[2], "Red Laser", superseded=True)
    await _prediction(owner_engine, lab, c, "red")
    await _prediction(owner_engine, lab, frames[0], None)
    await _prediction(owner_engine, lab, frames[1], "green", x=None)  # no dot
    number = await _number(owner_engine, mission_id)

    body = (await _get(client, f"/tenants/lab/missions/{number}")).json()

    assert body["laser_colors"] == {
        "labels": {"red": 2, "green": 1, "unread": 0},
        "predictions": {"red": 1, "green": 0, "unread": 1},
        "automatic": {"red": 1, "green": 0, "unread": 0},
    }


async def test_an_observation_shows_its_labels_colour(
    client, seed_memberships, owner_engine
):
    from observation_seed import ALICE, lite

    lab = (await seed_memberships({ALICE: {"lab": "member"}}))["lab"]
    capture_id, fish_id = await lite(owner_engine, lab)
    await exec_(owner_engine, "UPDATE laser_labels SET label = 'Green Laser'")

    body = (
        await _get(client, f"/tenants/lab/observations/{capture_id}/{fish_id}")
    ).json()

    assert body["laser"]["color"] == "green"
