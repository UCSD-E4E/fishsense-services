"""Linking v1's measurements to the inputs v1 did not record (2026-10-22).

v1 kept only the calibration on a measurement, so a migrated fish's evidence
showed no depth, dot, head or tail. Its depth is recoverable exactly (the one
v1 depth under that calibration, which names its laser label); its head/tail
label is the one whose length, recomputed from that depth, reproduces v1's.

A plan is read-only and carries a fingerprint; applying needs the fingerprint
of the plan that was reviewed, writes under guards on the exact old values,
and writes nothing when anything moved since the review.
"""

import re

import pytest
from sqlalchemy import text

from depth_measure_seed import (
    K,
    calibrate,
    capture,
    depth,
    device,
    fish,
    head_tail_label,
    laser_label,
    measurement,
    mission,
    tenant,
)
from fishsense_services_api.automatic_validation import length
from fishsense_services_api.cli import main
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.observation_store import observation
from fishsense_services_api.v1_measurement_inputs import (
    PlanChanged,
    apply_v1_inputs,
    plan_v1_inputs,
)

DEPTH_M = 1.8
HEAD, TAIL = (1600.0, 1125.0), (2200.0, 1360.0)
#: What v1 stored: its own float arithmetic, a hair off ours.
V1_LENGTH = length(K, DEPTH_M, *HEAD, *TAIL) * (1 + 3e-7)


async def _v1_depth(owner_engine, tenant_id, capture_id, laser, calibration):
    """A depth as v1_migration wrote it: a v1 id, no core version."""
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO laser_depths (tenant_id, v1_id, capture_id, "
                    "laser_label_id, laser_calibration_id, depth_m, range_m) "
                    "VALUES (:t, :v1, :c, :l, :cal, :d, :d) RETURNING id"
                ),
                {
                    "t": tenant_id,
                    "v1": abs(hash(capture_id)) % 2**31,
                    "c": capture_id,
                    "l": laser,
                    "cal": calibration,
                    "d": DEPTH_M,
                },
            )
        ).scalar_one()


async def _v1_fish(owner_engine, tenant_id, *, labels=((HEAD, TAIL),), v1_length=None):
    """A migrated v1 measurement: calibration only, as v1_migration wrote it,
    beside v1's depth (naming its laser label) and the capture's labels."""
    mission_id = await mission(
        owner_engine, tenant_id, device_id=await device(owner_engine, tenant_id)
    )
    calibration = await calibrate(owner_engine, tenant_id, mission_id)
    capture_id = await capture(owner_engine, tenant_id, mission_id)
    laser = await laser_label(owner_engine, tenant_id, capture_id, x=1925.0, y=1310.0)
    depth_id = await _v1_depth(owner_engine, tenant_id, capture_id, laser, calibration)
    head_tails = [
        await head_tail_label(owner_engine, tenant_id, capture_id, head=h, tail=t)
        for h, t in labels
    ]
    fish_id = await fish(owner_engine, tenant_id)
    measurement_id = await measurement(
        owner_engine,
        tenant_id,
        capture_id,
        fish_id,
        calibration,
        length_m=v1_length or V1_LENGTH,
        v1_id=abs(hash(capture_id)) % 2**31,
    )
    return {
        "measurement": measurement_id,
        "capture": capture_id,
        "fish": fish_id,
        "depth": depth_id,
        "calibration": calibration,
        "laser": laser,
        "head_tails": head_tails,
    }


async def _links(owner_engine, measurement_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT laser_depth_id, laser_label_id, head_tail_label_id "
                    "FROM measurements WHERE id = :m"
                ),
                {"m": measurement_id},
            )
        ).one()


async def _plan(app_engine, tenant_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await plan_v1_inputs(conn, tenant_id)


async def _apply(owner_engine, tenant_id, expect):
    async with tenant_transaction(owner_engine, tenant_id) as conn:
        return await apply_v1_inputs(conn, tenant_id, expect=expect)


async def test_the_plan_links_v1s_depth_and_the_head_tail_that_reproduces_its_length(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)

    plan = await _plan(app_engine, lab)

    [link] = plan.links
    assert link.measurement_id == seeded["measurement"]
    assert link.laser_depth_id == seeded["depth"]
    assert link.laser_label_id == seeded["laser"]
    assert link.head_tail_label_id == seeded["head_tails"][0]
    assert link.relative_error == pytest.approx(3e-7, rel=1e-3)
    assert not link.leaves_current
    assert plan.skipped == ()
    # A plan writes nothing.
    assert tuple(await _links(owner_engine, seeded["measurement"])) == (None,) * 3


async def test_of_two_head_tail_labels_the_one_v1_measured_from_is_linked(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    relabelled = ((1500.0, 1100.0), (2300.0, 1400.0))
    seeded = await _v1_fish(owner_engine, lab, labels=(relabelled, (HEAD, TAIL)))

    [link] = (await _plan(app_engine, lab)).links

    assert link.head_tail_label_id == seeded["head_tails"][1]


async def test_a_fish_no_label_reproduces_is_skipped_with_its_reason(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab, v1_length=V1_LENGTH * 1.01)

    plan = await _plan(app_engine, lab)

    assert plan.links == ()
    [skip] = plan.skipped
    assert skip.measurement_id == seeded["measurement"]
    assert "no head/tail label reproduces" in skip.reason


async def test_two_labels_that_both_reproduce_are_ambiguous_and_skipped(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    await _v1_fish(owner_engine, lab, labels=((HEAD, TAIL), (HEAD, TAIL)))

    plan = await _plan(app_engine, lab)

    assert plan.links == ()
    [skip] = plan.skipped
    assert "2 head/tail labels reproduce" in skip.reason


async def test_v2_measurements_and_linked_v1_rows_are_left_alone(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)
    # A v2 measurement of the same fish names its own inputs.
    await measurement(
        owner_engine,
        lab,
        seeded["capture"],
        seeded["fish"],
        seeded["calibration"],
        laser_label_id=seeded["laser"],
        head_tail_label_id=seeded["head_tails"][0],
    )
    await _apply(owner_engine, lab, (await _plan(app_engine, lab)).fingerprint)

    again = await _plan(app_engine, lab)

    assert again.links == () and again.skipped == ()


async def test_a_link_that_would_take_a_fish_out_of_the_current_results_is_flagged(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE head_tail_labels SET superseded = true WHERE id = :h"),
            {"h": seeded["head_tails"][0]},
        )

    [link] = (await _plan(app_engine, lab)).links

    assert link.leaves_current


async def test_applying_the_reviewed_plan_fills_the_fishs_evidence(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)
    plan = await _plan(app_engine, lab)

    applied = await _apply(owner_engine, lab, plan.fingerprint)

    assert applied == 1
    assert tuple(await _links(owner_engine, seeded["measurement"])) == (
        seeded["depth"],
        seeded["laser"],
        seeded["head_tails"][0],
    )
    async with tenant_transaction(app_engine, lab) as conn:
        found = await observation(conn, lab, seeded["capture"], seeded["fish"])
    assert found.laser.depth_m == DEPTH_M
    assert found.laser.dot == (1925.0, 1310.0)
    assert found.laser.head == HEAD and found.laser.tail == TAIL


async def test_applying_a_plan_that_changed_since_review_writes_nothing(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    first = await _v1_fish(owner_engine, lab)
    reviewed = await _plan(app_engine, lab)
    await _v1_fish(owner_engine, lab)

    with pytest.raises(PlanChanged):
        await _apply(owner_engine, lab, reviewed.fingerprint)

    assert tuple(await _links(owner_engine, first["measurement"])) == (None,) * 3


async def test_a_v2_depth_beside_v1s_is_not_the_one_v1_measured_from(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)
    await depth(
        owner_engine,
        lab,
        seeded["capture"],
        seeded["laser"],
        seeded["calibration"],
        depth_m=DEPTH_M * 1.1,
    )

    [link] = (await _plan(app_engine, lab)).links

    assert link.laser_depth_id == seeded["depth"]


# ``fishsense-services-api backfill-v1-measurement-inputs``: a dry run by
# default, as the schema owner (the apply updates `measurements`).


async def test_the_command_dry_runs_then_applies_what_was_reviewed(
    owner_engine, owner_url, monkeypatch, capsys
):
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)
    await _v1_fish(owner_engine, lab, v1_length=V1_LENGTH * 1.01)
    command = ["backfill-v1-measurement-inputs", "--tenant", "lab"]

    assert await main(command) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "1 to link, 0 of them leaving the current results" in out
    assert "1 skipped" in out and "no head/tail label reproduces" in out
    assert tuple(await _links(owner_engine, seeded["measurement"])) == (None,) * 3
    fingerprint = re.search(r"--apply ([0-9a-f]{16})", out)[1]

    assert await main([*command, "--apply", fingerprint]) == 0

    assert "linked 1" in capsys.readouterr().out
    assert (await _links(owner_engine, seeded["measurement"]))[2] == (
        seeded["head_tails"][0]
    )


async def test_the_command_refuses_a_plan_nobody_reviewed(
    owner_engine, owner_url, monkeypatch, capsys
):
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)
    lab = await tenant(owner_engine)
    seeded = await _v1_fish(owner_engine, lab)

    assert (
        await main(
            [
                "backfill-v1-measurement-inputs",
                "--tenant",
                "lab",
                "--apply",
                "0123456789abcdef",
            ]
        )
        == 1
    )

    assert "review it again" in capsys.readouterr().err
    assert tuple(await _links(owner_engine, seeded["measurement"])) == (None,) * 3
