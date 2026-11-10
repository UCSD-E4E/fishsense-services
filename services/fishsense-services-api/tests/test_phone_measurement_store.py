"""The phone measurement's database side (0088; the owner, 2026-11-09).

The server measures a phone capture that was never measured (a measurement the
phone synced counts), as FishSense Mobile measures it. The result is the
server's and unvalidated (the owner, 2026-11-09): its own append-only table,
never a `measurements` row, so it can't reach `current_measurements`, the
observations or a public total until a person reviews it.
"""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_phone_missions import T0, _capture

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import (
    DeviceMeasurement,
    commit_capture,
)
from fishsense_services_api.phone_measurement_store import (
    PHONE_STATUSES,
    PhoneMeasurementCatalog,
    PhoneMeasurementMask,
    PhoneMeasurementResult,
)

ORCHESTRATOR = "service:orchestrator"


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ORCHESTRATOR: {"lab": "member", "ccfrp": "member"}})


@pytest.fixture
def catalog(app_engine):
    return PhoneMeasurementCatalog(app_engine, sub=ORCHESTRATOR)


async def _phone(app_engine, tenant_id, *, measured=False, at=T0, files=None):
    capture = _capture(None, at=at)
    capture = replace(capture, imported_from=f"dataset#photos/{uuid.uuid4()}")
    if files is not None:
        capture = replace(
            capture, files=tuple(f for f in capture.files if f.role in files)
        )
    measurements = []
    if measured:
        measurements = [DeviceMeasurement(
            client_measurement_uuid=uuid.uuid4(), measured_at=at, length_m=0.3,
            snout=(10.0, 20.0), fork=(30.0, 40.0), origin="auto", algorithm=None,
            algorithm_version=None, core_version=None, model_version=None,
        )]  # fmt: skip
    async with tenant_transaction(app_engine, tenant_id) as conn:
        committed = await commit_capture(conn, tenant_id, capture, measurements)
    return committed.capture_id, capture


def _measured(capture_id, **changes):
    result = PhoneMeasurementResult(
        capture_id=capture_id, status="measured", length_m=0.284,
        snout_x=530.0, snout_y=690.0, fork_x=1500.0, fork_y=740.0,
        snout_depth_m=0.38, fork_depth_m=0.44, model_id="fishial/1@f721445f37db",
        core_version="4.3.2", algorithm="fishsense-mobile@cd120f4", error=None,
        mask=PhoneMeasurementMask(
            bucket="fishsense-tenant-lab", key=f"masks/phone/{capture_id}/{uuid.uuid4()}.png",
            sha256="d" * 64, size=1234, width=1920, height=1440,
        ),
    )  # fmt: skip
    return replace(result, **changes)


async def test_the_work_is_the_phone_captures_never_measured(
    tenants, catalog, app_engine
):
    owed, _ = await _phone(app_engine, tenants["lab"])
    await _phone(app_engine, tenants["lab"], measured=True)

    missions = await catalog.owed_missions(tenants["lab"])

    assert [m.captures for m in missions] == [1]
    [page] = [await catalog.owed_captures(tenants["lab"], missions[0].mission_id)]
    assert [c.capture_id for c in page] == [owed]


async def test_each_capture_comes_with_its_files_its_depth_and_its_k(
    tenants, catalog, app_engine
):
    capture_id, capture = await _phone(app_engine, tenants["lab"])
    [mission] = await catalog.owed_missions(tenants["lab"])

    [c] = await catalog.owed_captures(tenants["lab"], mission.mission_id)

    keys = {f.role: f.key for f in capture.files}
    assert (c.rgb_key, c.depth_key) == (keys["rgb_jpeg"], keys["depth_f32"])
    assert c.bucket == "fishsense-tenant-lab"
    assert (c.depth_width, c.depth_height, c.depth_row_bytes) == (256, 192, 1024)
    assert c.intrinsics == capture.intrinsics


async def test_a_capture_missing_its_depth_is_not_work(tenants, catalog, app_engine):
    """A phone capture always has its RGB (`commit_capture` requires it)."""
    await _phone(app_engine, tenants["lab"], files={"rgb_jpeg", "confidence_u8"})

    assert await catalog.owed_missions(tenants["lab"]) == []


async def test_a_capture_with_a_server_result_is_not_work_again(
    tenants, catalog, app_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    await catalog.record(tenants["lab"], [_measured(capture_id, status="no_fish", length_m=None,
                                                    snout_x=None, snout_y=None, fork_x=None,
                                                    fork_y=None, snout_depth_m=None,
                                                    fork_depth_m=None, mask=None)])  # fmt: skip

    assert await catalog.owed_missions(tenants["lab"]) == []


async def test_captures_page_in_order(tenants, catalog, app_engine):
    ids = sorted([(await _phone(app_engine, tenants["lab"], at=T0 + timedelta(minutes=i)))[0]
                  for i in range(5)])  # fmt: skip
    [mission] = await catalog.owed_missions(tenants["lab"])

    first = await catalog.owed_captures(tenants["lab"], mission.mission_id, limit=2)
    rest = await catalog.owed_captures(tenants["lab"], mission.mission_id,
                                       after=first[-1].capture_id, limit=10)  # fmt: skip

    assert [c.capture_id for c in first + rest] == ids


async def test_a_result_is_recorded_with_its_mask_and_is_current(
    tenants, catalog, app_engine, owner_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    result = _measured(capture_id)

    assert await catalog.record(tenants["lab"], [result]) == 1

    async with owner_engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT * FROM current_phone_measurements WHERE capture_id = :c"),
            {"c": capture_id})).one()  # fmt: skip
    assert (row.status, row.length_m, row.snout_x, row.fork_y) == (
        "measured",
        0.284,
        530.0,
        740.0,
    )
    assert (row.mask_key, row.mask_width, row.model_id) == (
        result.mask.key,
        1920,
        "fishial/1@f721445f37db",
    )


async def test_the_latest_result_is_current_and_history_stays(
    tenants, catalog, app_engine, owner_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    await catalog.record(tenants["lab"], [_measured(capture_id, length_m=0.3)])
    await catalog.record(tenants["lab"], [_measured(capture_id, length_m=0.31)])

    async with owner_engine.connect() as conn:
        lengths = (await conn.execute(text(
            "SELECT length_m FROM phone_measurements WHERE capture_id = :c ORDER BY seq"),
            {"c": capture_id})).scalars().all()  # fmt: skip
        current = (await conn.execute(text(
            "SELECT length_m FROM current_phone_measurements WHERE capture_id = :c"),
            {"c": capture_id})).scalar_one()  # fmt: skip
    assert lengths == [0.3, 0.31] and current == 0.31


async def test_a_result_is_never_a_measurement(
    tenants, catalog, app_engine, owner_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])

    await catalog.record(tenants["lab"], [_measured(capture_id)])

    async with owner_engine.connect() as conn:
        n = (await conn.execute(text(
            "SELECT count(*) FROM measurements WHERE capture_id = :c"),
            {"c": capture_id})).scalar_one()  # fmt: skip
    assert n == 0


async def test_a_capture_that_is_not_a_phone_capture_of_the_tenant_is_refused(
    tenants, catalog, app_engine
):
    elsewhere, _ = await _phone(app_engine, tenants["ccfrp"])

    with pytest.raises(ValueError):
        await catalog.record(tenants["lab"], [_measured(elsewhere)])
    with pytest.raises(ValueError):
        await catalog.record(tenants["lab"], [_measured(uuid.uuid4())])


async def test_a_mask_stored_under_another_capture_is_refused(
    tenants, catalog, app_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    other, _ = await _phone(app_engine, tenants["lab"])
    result = _measured(capture_id)
    wrong = replace(
        result, mask=replace(result.mask, key=f"masks/phone/{other}/{uuid.uuid4()}.png")
    )

    with pytest.raises(ValueError):
        await catalog.record(tenants["lab"], [wrong])


async def test_only_a_result_that_segmented_a_fish_names_a_mask(
    tenants, catalog, app_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    no_fish = _measured(capture_id, status="no_fish", length_m=None, snout_x=None,
                        snout_y=None, fork_x=None, fork_y=None, snout_depth_m=None,
                        fork_depth_m=None)  # fmt: skip

    with pytest.raises(DBAPIError):
        await catalog.record(tenants["lab"], [no_fish])


async def test_a_measured_result_needs_its_length_and_points(
    tenants, catalog, app_engine
):
    capture_id, _ = await _phone(app_engine, tenants["lab"])

    with pytest.raises(DBAPIError):
        await catalog.record(tenants["lab"], [_measured(capture_id, length_m=None)])
    with pytest.raises(DBAPIError):
        await catalog.record(tenants["lab"], [_measured(capture_id, fork_y=None)])


async def test_another_tenant_sees_none_of_it(tenants, catalog, app_engine):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    await catalog.record(tenants["lab"], [_measured(capture_id)])

    async with tenant_transaction(app_engine, tenants["ccfrp"]) as conn:
        n = (
            await conn.execute(text("SELECT count(*) FROM phone_measurements"))
        ).scalar_one()
    assert n == 0


async def test_results_are_append_only(tenants, catalog, app_engine):
    capture_id, _ = await _phone(app_engine, tenants["lab"])
    await catalog.record(tenants["lab"], [_measured(capture_id)])

    for statement in ("UPDATE phone_measurements SET length_m = 1",
                      "DELETE FROM phone_measurements"):  # fmt: skip
        with pytest.raises(DBAPIError):
            async with tenant_transaction(app_engine, tenants["lab"]) as conn:
                await conn.execute(text(statement))


def test_the_statuses_are_the_contracts():
    """Repeated, not imported: the API image has no contracts package. The
    orchestrator's tests pin the two lists equal."""
    assert PHONE_STATUSES == ("measured", "no_fish", "headtail_failed",
                              "depth_failed", "image_unavailable", "decode_failed")  # fmt: skip
