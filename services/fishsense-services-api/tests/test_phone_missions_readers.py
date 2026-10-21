"""What reads missions, with a phone mission among them (0057).

A phone mission has no folder. The code that reads a Lite folder (the port
and purpose backfill, deployment inference, automatic results, the public map)
leaves phone missions alone, and the portal shows them for what they are.
A phone has no camera port, so phone missions aren't counted as missions
whose port is unknown.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from test_phone_missions import ALICE, _capture, _commit, _mission_of

from fishsense_services_api.automatic_results_store import automatic_run_plan
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.deployment_inference import plan_inference
from fishsense_services_api.mission_backfill import plan_backfill
from fishsense_services_api.mobile_capture_store import (
    DeviceMeasurement,
    commit_capture,
)
from fishsense_services_api.portal_store import list_missions, mission_detail
from fishsense_services_api.public_store import public_summary
from fishsense_services_api.summary_store import tenant_summary

T0 = datetime(2026, 10, 11, 17, 30, tzinfo=UTC)


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member"}})


@pytest.fixture
async def phone(tenants, app_engine, owner_engine):
    """A phone mission in lab, and its number."""
    async with owner_engine.connect() as conn:
        alice = (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": ALICE}
            )
        ).scalar_one()
    capture = _capture(alice)
    measurement = DeviceMeasurement(
        client_measurement_uuid=capture.client_capture_uuid,
        measured_at=T0,
        length_m=0.42,
        snout=(100.0, 200.0),
        fork=(900.0, 210.0),
        origin="auto",
        algorithm="fishsense-mobile.compute_length",
        algorithm_version="1.0.0",
        core_version=None,
        model_version=None,
    )
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        committed = await commit_capture(conn, tenants["lab"], capture, [measurement])
    return (await _mission_of(owner_engine, committed.capture_id)).number


async def _as_lab(app_engine, tenants, read):
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        return await read(conn)


async def test_the_portal_lists_it_as_a_phone_mission(phone, tenants, app_engine):
    missions = await _as_lab(
        app_engine, tenants, lambda c: list_missions(c, tenants["lab"])
    )

    (found,) = [m for m in missions if m.number == phone]
    assert found.source == "mobile"
    assert found.captures == 1


async def test_its_page_has_no_folder(phone, tenants, app_engine):
    detail = await _as_lab(
        app_engine, tenants, lambda c: mission_detail(c, tenants["lab"], phone)
    )

    assert detail.source_path is None
    assert detail.summary.source == "mobile"


async def test_its_port_is_not_counted_as_unknown(phone, tenants, app_engine):
    summary = await _as_lab(
        app_engine, tenants, lambda c: tenant_summary(c, tenants["lab"])
    )

    assert summary.missions == 1
    assert summary.unknown_ports == 0


async def test_the_port_and_purpose_backfill_leaves_it_alone(
    phone, tenants, app_engine
):
    plan = await _as_lab(
        app_engine,
        tenants,
        lambda c: plan_backfill(c, tenants["lab"], ingested_before=T0),
    )

    assert phone not in {m.number for m in plan.missions}
    assert phone not in plan.left_unknown


async def test_deployment_inference_leaves_it_alone(phone, tenants, app_engine):
    plan = await _as_lab(
        app_engine, tenants, lambda c: plan_inference(c, tenants["lab"])
    )

    assert phone not in {n for t in plan.trips for n in t.missions}


async def test_automatic_results_do_not_plan_it(phone, tenants, app_engine):
    plan = await _as_lab(
        app_engine,
        tenants,
        lambda c: automatic_run_plan(c, tenants["lab"], [phone], headtail_version=1),
    )

    assert plan.missions == []
    assert plan.missing == [phone]


async def test_the_public_map_skips_a_mission_without_a_folder(
    phone, tenants, app_engine, owner_engine
):
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE tenants SET public_totals = true"))
    async with app_engine.connect() as conn:
        summary = await public_summary(conn)

    assert summary.wild_fish == 1
    assert summary.regions == []
