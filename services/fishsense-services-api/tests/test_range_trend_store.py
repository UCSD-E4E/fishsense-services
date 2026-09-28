"""What the range-trend audit reads for one dive, tenant-scoped.

v1's scripts/audit_length_range_trend.py (fishsense-lite@77e8f8e5) read,
through the SDK: the dive's resolved laser extrinsics (the baseline is
`hypot(laser_position[:2])`), its measurements, its laser depths with
`depth_m > 0`, and its completed, non-superseded species labels parsed with
`parse_model_name`. In v2 each has a home:

* the resolved extrinsics are the dive's `effective_laser_calibrations` row --
  its own accepted, plausible calibration, else its source dive's;
* the measurements are the capture's *current* server measurements
  (`current_measurements`) on canonical captures;
* the depth is the capture's `current_laser_depths` row;
* the name is `fish_model_name` -- `parse_model_name` in SQL, pinned to it by
  tests/test_depth_measure_schema.py -- of the capture's latest completed,
  non-superseded species label.
"""

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrate,
    calibrated_dive,
    capture,
    depth,
    device,
    dive,
    exec_,
    fish,
    forget_identities,
    laser_label,
    measurement,
    species_label,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.range_trend_store import range_trend_inputs

WEASLY = "Fish Model, Weasly Fish"


async def _number(owner_engine, dive_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT number FROM dives WHERE id = :d"), {"d": dive_id}
            )
        ).scalar_one()


async def _fish_number(owner_engine, fish_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT number FROM fish WHERE id = :f"), {"f": fish_id}
            )
        ).scalar_one()


async def _inputs(app_engine, tenant_id, dive_number):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await range_trend_inputs(conn, tenant_id, dive_number)


async def _measured(
    owner_engine,
    lab,
    dive_id,
    calibration,
    fish_id,
    *,
    content=WEASLY,
    length_m=0.3,
    depth_m=1.5,
    canonical=True,
):
    """A capture with a species label, a laser depth and a server measurement."""
    capture_id = await capture(owner_engine, lab, dive_id, canonical=canonical)
    laser = await laser_label(owner_engine, lab, capture_id)
    if content is not None:
        await species_label(owner_engine, lab, capture_id, content)
    if depth_m is not None:
        await depth(owner_engine, lab, capture_id, laser, calibration, depth_m=depth_m)
    await measurement(
        owner_engine,
        lab,
        capture_id,
        fish_id,
        calibration,
        length_m=length_m,
        laser_label_id=laser,
    )
    return capture_id


async def test_reads_the_calibration_measurements_depths_and_rigid_names(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    weasly = await fish(owner_engine, lab, model="Weasly Fish")
    wild = await fish(owner_engine, lab)
    rigid = await _measured(
        owner_engine, lab, dive_id, calibration, weasly, length_m=0.31, depth_m=1.2
    )
    real = await _measured(
        owner_engine,
        lab,
        dive_id,
        calibration,
        wild,
        content="Fish, Hogfish (Lachnolaimus maximus)",
        length_m=0.42,
        depth_m=2.5,
    )

    inputs = await _inputs(app_engine, lab, await _number(owner_engine, dive_id))

    assert inputs.laser_position == [0.1, 0.0, 0.0]
    assert inputs.baseline_m == pytest.approx(0.1)
    assert sorted((m.capture_id, m.length_m) for m in inputs.measurements) == sorted(
        [(rigid, 0.31), (real, 0.42)]
    )
    by_capture = {m.capture_id: m for m in inputs.measurements}
    assert by_capture[real].fish_number == await _fish_number(owner_engine, wild)
    assert inputs.depth_by_capture == {rigid: 1.2, real: 2.5}
    assert inputs.name_by_capture == {rigid: "Weasly Fish", real: None}


async def test_a_borrowed_calibration_is_the_one_resolved(owner_engine, app_engine):
    """v1's `get_laser_extrinsics` resolved a borrower to its source's fit."""
    lab = await tenant(owner_engine)
    source, _ = await calibrated_dive(owner_engine, lab)
    await exec_(
        owner_engine,
        "UPDATE laser_calibrations SET laser_position = '[0.12, 0.0, 0.0]' "
        "WHERE dive_id = :d",
        d=source,
    )
    borrower = await dive(
        owner_engine,
        lab,
        device_id=await device(owner_engine, lab),
        source_dive=source,
    )

    inputs = await _inputs(app_engine, lab, await _number(owner_engine, borrower))

    assert inputs.baseline_m == pytest.approx(0.12)


async def test_a_dive_without_a_usable_calibration_has_no_baseline(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id = await dive(owner_engine, lab, device_id=await device(owner_engine, lab))
    await calibrate(owner_engine, lab, dive_id, outcome="refused")

    inputs = await _inputs(app_engine, lab, await _number(owner_engine, dive_id))

    assert inputs.laser_position is None
    assert inputs.baseline_m is None


async def test_only_current_measurements_on_canonical_captures_are_read(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    weasly = await fish(owner_engine, lab, model="Weasly Fish")
    # measured under the calibration the dive no longer resolves to
    stale = await _measured(owner_engine, lab, dive_id, old, weasly)
    new = await calibrate(owner_engine, lab, dive_id)
    current = await _measured(owner_engine, lab, dive_id, new, weasly)
    await _measured(owner_engine, lab, dive_id, new, weasly, canonical=False)

    inputs = await _inputs(app_engine, lab, await _number(owner_engine, dive_id))

    assert [m.capture_id for m in inputs.measurements] == [current]
    assert stale not in inputs.depth_by_capture


async def test_names_come_only_from_completed_live_species_labels(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    # low priority: a relabel does not unbind the measurement (see 0028)
    dive_id, calibration = await calibrated_dive(owner_engine, lab, priority="low")
    weasly = await fish(owner_engine, lab, model="Weasly Fish")
    superseded = await _measured(owner_engine, lab, dive_id, calibration, weasly)
    await exec_(
        owner_engine,
        "UPDATE species_labels SET superseded = true WHERE capture_id = :c",
        c=superseded,
    )
    incomplete = await _measured(owner_engine, lab, dive_id, calibration, weasly)
    await exec_(
        owner_engine,
        "UPDATE species_labels SET completed = false WHERE capture_id = :c",
        c=incomplete,
    )
    relabelled = await _measured(owner_engine, lab, dive_id, calibration, weasly)
    await species_label(owner_engine, lab, relabelled, "Calibration Targets, Box")

    inputs = await _inputs(app_engine, lab, await _number(owner_engine, dive_id))

    assert superseded not in inputs.name_by_capture
    assert incomplete not in inputs.name_by_capture
    assert inputs.name_by_capture[relabelled] == "Box"  # the latest label


async def test_another_tenants_dive_and_an_unknown_number_read_as_none(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    other = await tenant(owner_engine, "other")
    theirs, _ = await calibrated_dive(owner_engine, other)
    number = await _number(owner_engine, theirs)

    assert await _inputs(app_engine, lab, number) is None
    assert await _inputs(app_engine, lab, number + 1000) is None
