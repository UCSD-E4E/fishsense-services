"""Automatic lengths, a page at a time (the owner, 2026-10-24).

The list ``automatic_results_export`` holds, for a tenant or a mission, with
each length's review and the human length on its frame. It must keep the
export's exclusions (0043, 0064) while reading the current views by capture id
for speed, so the tests here pin the rows against the export itself.
"""

from __future__ import annotations

import uuid

import pytest
from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrate,
    exec_,
    fish,
    forget_identities,
    measurement,
    tenant,
)
from sqlalchemy import text
from test_automatic_results_store import HOGFISH, _in, _measured
from test_automatic_review_store import _render
from test_slate_presence_store import _row as _slate_row

from fishsense_services_api.automatic_lengths_store import (
    automatic_lengths,
    parse_cursor,
)
from fishsense_services_api.automatic_results_store import (
    AutomaticSpeciesRow,
    persist_automatic_species,
)
from fishsense_services_api.slate_presence_store import persist_slate_presence


async def _species(owner_engine, app_engine, lab, mission_id, c, choice=HOGFISH):
    async with owner_engine.connect() as conn:
        prediction = (
            await conn.execute(
                text(
                    "SELECT id FROM current_automatic_head_tail_predictions "
                    "WHERE capture_id = :c"
                ),
                {"c": c},
            )
        ).scalar_one()
    await _in(app_engine, lab, persist_automatic_species, mission_id, [
        AutomaticSpeciesRow(
            capture_id=c, automatic_head_tail_prediction_id=prediction,
            status="predicted", predictor_version=1, model_id="bioclip/2.5@x",
            predicted_choice=choice, top1_probability=0.7, margin=0.4,
            top5=[{"choice": choice, "probability": 0.7}],
        )
    ])  # fmt: skip


async def _reviewer(owner_engine, lab) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO users (sub) VALUES (:s) RETURNING id"),
                {"s": f"reviewer-{uuid.uuid4().hex[:6]}"},
            )
        ).scalar_one()


async def _review(owner_engine, lab, c, reviewer, kind, action, level, **extra):
    columns = {"tenant_id": lab, "capture_id": c, "reviewer_id": reviewer,
               "kind": kind, "action": action, "level": level, **extra}  # fmt: skip
    await exec_(
        owner_engine,
        f"INSERT INTO reviews ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + k for k in columns)})",
        **columns,
    )


async def _export(owner_engine, lab) -> list[tuple]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT capture_id, length_m FROM automatic_results_export "
                "WHERE tenant_id = :t ORDER BY dive_number, capture_number"
            ),
            {"t": lab},
        )
        return [tuple(r) for r in rows]


async def test_a_length_comes_with_what_judges_it(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _species(owner_engine, app_engine, lab, mission_id, c)

    page = await _in(app_engine, lab, automatic_lengths)

    (row,) = page.lengths
    assert (row.capture_id, row.length_m, row.depth_m) == (c, 0.42, 2.1)
    assert row.calibration_source == "label_free"
    assert row.species == HOGFISH
    assert row.scientific_name == "Lachnolaimus maximus"
    assert (row.species_probability, row.species_margin) == (0.7, 0.4)
    assert (row.laser_confidence, row.sam_score) == (0.9, 0.83)
    assert row.human_length_m is None
    assert row.mission_human_measured is False
    assert row.reviews == {}
    assert page.next is None


async def test_a_length_out_of_distribution_for_its_species_is_flagged(
    owner_engine, app_engine
):
    """2026-10-30: against the species BioCLIP names (`species_length`)."""
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _species(owner_engine, app_engine, lab, mission_id, c)

    (row,) = (await _in(app_engine, lab, automatic_lengths)).lengths
    assert (row.species_length_flag, row.species_max_length_m) == (None, 0.91)

    await exec_(owner_engine, "UPDATE automatic_measurements SET length_m = 1.0 "
                "WHERE capture_id = :c", c=c)  # fmt: skip
    (row,) = (await _in(app_engine, lab, automatic_lengths)).lengths
    assert row.species_length_flag == "over_max"


async def test_the_rows_are_the_exports(owner_engine, app_engine):
    """Every current automatic length the export holds, and no other."""
    lab = await tenant(owner_engine)
    for _ in range(3):
        await _measured(owner_engine, app_engine, lab)

    page = await _in(app_engine, lab, automatic_lengths)

    assert [(r.capture_id, r.length_m) for r in page.lengths] == await _export(
        owner_engine, lab
    )
    assert len(page.lengths) == 3


async def test_a_slate_frame_is_left_out_as_the_export_leaves_it(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _in(app_engine, lab, persist_slate_presence, mission_id, [_slate_row(c)])

    page = await _in(app_engine, lab, automatic_lengths)

    assert page.lengths == []
    assert await _export(owner_engine, lab) == []


async def test_a_stale_length_is_left_out(owner_engine, app_engine):
    """A newer head/tail makes the length stale: it isn't current."""
    from test_automatic_results_store import _predict, _predicted

    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _predict(app_engine, lab, mission_id, _predicted(c, head_x=1700.0))

    assert (await _in(app_engine, lab, automatic_lengths)).lengths == []


async def test_the_human_length_on_the_frame_and_the_missions_flag(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    other_mission, _ = await _measured(owner_engine, app_engine, lab)
    cal = await calibrate(owner_engine, lab, mission_id)
    await measurement(owner_engine, lab, c, await fish(owner_engine, lab), cal,
                      length_m=0.39)  # fmt: skip

    page = await _in(app_engine, lab, automatic_lengths)

    by_mission = {r.capture_id: r for r in page.lengths}
    assert by_mission[c].human_length_m == 0.39
    assert by_mission[c].mission_human_measured is True
    others = [r for r in page.lengths if r.capture_id != c]
    assert [r.mission_human_measured for r in others] == [False]


async def test_one_mission(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _measured(owner_engine, app_engine, lab)

    page = await _in(app_engine, lab, automatic_lengths, mission_id=mission_id)

    assert [r.capture_id for r in page.lengths] == [c]


async def test_one_species_by_scientific_name(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    hog_mission, hog = await _measured(owner_engine, app_engine, lab)
    other_mission, other = await _measured(owner_engine, app_engine, lab)
    await _species(owner_engine, app_engine, lab, hog_mission, hog)
    await _species(owner_engine, app_engine, lab, other_mission, other,
                   choice="Fish, Goliath Grouper (Epinephelus itajara)")  # fmt: skip

    page = await _in(app_engine, lab, automatic_lengths, species="Lachnolaimus maximus")

    assert [r.capture_id for r in page.lengths] == [hog]


async def test_a_species_once_predicted_but_no_longer_is_left_out(
    owner_engine, app_engine
):
    """The cheap choice of captures looks at any prediction ever; the current
    one decides."""
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _species(owner_engine, app_engine, lab, mission_id, c)
    await _species(owner_engine, app_engine, lab, mission_id, c,
                   choice="Fish, Goliath Grouper (Epinephelus itajara)")  # fmt: skip

    page = await _in(app_engine, lab, automatic_lengths, species="Lachnolaimus maximus")

    assert page.lengths == []


async def test_pages_follow_one_another_to_the_end(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    for _ in range(3):
        await _measured(owner_engine, app_engine, lab)

    seen, after = [], None
    for _ in range(5):
        page = await _in(
            app_engine, lab, automatic_lengths, after=parse_cursor(after), limit=2
        )
        seen += [(r.capture_id, r.length_m) for r in page.lengths]
        if page.next is None:
            break
        after = page.next

    assert seen == await _export(owner_engine, lab)


@pytest.mark.parametrize("bad", ["", "3", "a:b", "3:", ":4", "3:4:5", "-1:2"])
def test_a_cursor_is_two_numbers(bad):
    with pytest.raises(ValueError):
        parse_cursor(bad)


async def test_a_review_that_counts_decides_and_a_newer_one_waits(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    trusted = await _reviewer(owner_engine, lab)
    learner = await _reviewer(owner_engine, lab)
    await _render(owner_engine, lab, c)
    async with owner_engine.connect() as conn:
        render = (
            await conn.execute(
                text("SELECT id FROM capture_renders WHERE capture_id = :c"), {"c": c}
            )
        ).scalar_one()
    points = {"head_x": 1.0, "head_y": 2.0, "tail_x": 3.0, "tail_y": 4.0}
    await _review(owner_engine, lab, c, trusted, "head_tail", "fix", "trusted",
                  render_id=render, **points)  # fmt: skip
    await _review(owner_engine, lab, c, learner, "head_tail", "reject", "learning",
                  reason="bad_mask")  # fmt: skip
    await _review(owner_engine, lab, c, learner, "laser", "accept", "learning",
                  render_id=render, x=5.0, y=6.0)  # fmt: skip

    (row,) = (await _in(app_engine, lab, automatic_lengths)).lengths

    head_tail, laser = row.reviews["head_tail"], row.reviews["laser"]
    assert (head_tail.action, head_tail.pending) == ("fix", True)
    assert (laser.action, laser.pending) == (None, True)
    assert "species" not in row.reviews


async def test_another_tenants_lengths_are_not_there(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    other = await tenant(owner_engine, "partner")
    await _measured(owner_engine, app_engine, lab)

    assert (await _in(app_engine, other, automatic_lengths)).lengths == []
