"""The laser-depth and stage-14 persists, fed what actually crosses Temporal.

The persist checks the processor's echo of its inputs against the open work
by equality: label ids as UUIDs, dots and keypoints as floats. Everything the
store is handed has been through Temporal twice -- the resolved work out to
the processor, its result back -- as JSON, via `pydantic_data_converter`. So a
regression there (an id decoded as a `str`, a float that does not round-trip)
would not fail a store test fed Python objects: it would make every result
"stale", and the dives would be re-selected forever with nothing written.

These run the orchestrator's own resolve and persist activities over the
real catalogs, with every argument and result encoded and decoded the way the
worker does, against real Postgres under RLS. The processor is stood in for:
its geometry is its own suite's.
"""

import uuid

from sqlalchemy import text
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    capture,
    exec_,
    forget_identities,
    laser_label,
    measurable_capture,
)
from fishsense_services_api.laser_depth_store import LaserDepthCatalog
from fishsense_services_api.measurement_store import MeasurementCatalog
from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsResult,
    LaserDepth,
    LaserDepthOutcome,
)
from fishsense_services_contracts.measurement import FishLength, MeasureFishResult
from fishsense_services_orchestrator.laser_depth.activities import (
    DiveTarget,
    LaserDepthActivities,
    LaserDepthResolution,
)
from fishsense_services_orchestrator.measurement.activities import (
    MeasurementActivities,
    MeasurementResolution,
)

ORCHESTRATOR = "service:fishsense-orchestrator"
#: Pixels no decimal shortcut can carry exactly: 0.1 + 0.2, and 17 digits.
AWKWARD_X, AWKWARD_Y = 1234.5678901234567, 0.1 + 0.2


def _across(values: list, types: list) -> list:
    """What the other side of Temporal receives: encoded as the worker's
    data converter encodes, decoded against the declared types."""
    converter = pydantic_data_converter.payload_converter
    return converter.from_payloads(converter.to_payloads(values), types)


async def test_a_depth_that_crossed_temporal_answers_its_work(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, capture_id, x=AWKWARD_X, y=AWKWARD_Y)
    activities = LaserDepthActivities(
        catalog=LaserDepthCatalog(app_engine, sub=ORCHESTRATOR)
    )
    env = ActivityEnvironment()

    (target,) = _across([DiveTarget(lab, dive_id)], [DiveTarget])
    (resolution,) = _across(
        [await env.run(activities.resolve_laser_depth_inputs, target)],
        [LaserDepthResolution],
    )
    payload = resolution.payload
    (dot,) = payload.captures[0].laser_labels
    result = ComputeLaserDepthsResult(
        dive_id=payload.dive_id,
        core_version="4.1.0",
        captures=[
            LaserDepthOutcome(
                capture_id=payload.captures[0].capture_id,
                depth=LaserDepth(
                    laser_label_id=dot.laser_label_id,
                    x=dot.x,
                    y=dot.y,
                    depth_m=1.2345678901234567,
                    range_m=1.25,
                    residual_m=3e-6,
                ),
                refusals=[],
            )
        ],
    )
    args = _across(
        [target, payload.calibration.laser_calibration_id, result],
        [DiveTarget, uuid.UUID, ComputeLaserDepthsResult],
    )

    persisted = await env.run(activities.persist_laser_depths, *args)

    assert (persisted.computed, persisted.skipped_stale) == (1, 0)
    async with owner_engine.connect() as conn:
        written = (
            await conn.execute(
                text("SELECT depth_m FROM laser_depths WHERE capture_id = :c"),
                {"c": capture_id},
            )
        ).scalar_one()
    assert written == 1.2345678901234567


async def test_a_length_that_crossed_temporal_answers_its_work(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    capture_id = await measurable_capture(
        owner_engine, lab, dive_id, "Fish Model, Grouper"
    )
    await exec_(
        owner_engine,
        "UPDATE laser_labels SET x = :x, y = :y WHERE capture_id = :c",
        x=AWKWARD_X,
        y=AWKWARD_Y,
        c=capture_id,
    )
    await exec_(
        owner_engine,
        "UPDATE head_tail_labels SET head_x = :x, tail_y = :y WHERE capture_id = :c",
        x=AWKWARD_X,
        y=AWKWARD_Y,
        c=capture_id,
    )
    activities = MeasurementActivities(
        catalog=MeasurementCatalog(app_engine, sub=ORCHESTRATOR)
    )
    env = ActivityEnvironment()

    (target,) = _across([DiveTarget(lab, dive_id)], [DiveTarget])
    (resolution,) = _across(
        [await env.run(activities.resolve_measurement_inputs, target)],
        [MeasurementResolution],
    )
    payload = resolution.payload
    (item,) = payload.captures
    result = MeasureFishResult(
        dive_id=payload.dive_id,
        algorithm="laser_depth_fronto_parallel",
        algorithm_version="1",
        core_version="4.1.0",
        captures=[
            FishLength(
                capture_id=item.capture_id,
                species_label_id=item.species_label_id,
                laser=item.laser,
                head_tail=item.head_tail,
                length_m=0.30000000000000004,
                depth_m=1.2,
                refusal=None,
            )
        ],
    )
    args = _across(
        [target, payload.calibration.laser_calibration_id, result],
        [DiveTarget, uuid.UUID, MeasureFishResult],
    )

    persisted = await env.run(activities.persist_measurements, *args)

    assert (persisted.measured, persisted.skipped_stale) == (1, 0)
    async with owner_engine.connect() as conn:
        written = (
            await conn.execute(
                text("SELECT length_m FROM measurements WHERE capture_id = :c"),
                {"c": capture_id},
            )
        ).scalar_one()
    assert written == 0.30000000000000004
