"""Phone captures grouped into missions (Deep water phase 7; the owner,
2026-10-21).

A FishSense Mobile capture joins the mission of the same phone's capture
nearest in time, if that one is at most **2 hours** away and at most **1 km**
off. Otherwise it starts a mission of its own. A capture without a location
can't be shown to be more than 1 km off, so time alone decides for it. A late
sync that falls between two missions joins the nearer one and never merges
them: missions keep their numbers.

A phone mission has no folder (`source = 'mobile'`, `source_path` NULL). It is
named for the phone's local day, using the `time_zone` the app sends, or UTC
when it sends none, and for the place.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import (
    CaptureFile,
    MobileCapture,
    commit_capture,
)

ALICE = "sub-alice"
T0 = datetime(2026, 10, 11, 17, 30, tzinfo=UTC)
#: La Jolla Cove, and points about 0.9 km and 1.6 km north of it.
COVE = (32.8501, -117.2730)
NEAR = (32.8582, -117.2730)
FAR = (32.8645, -117.2730)


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member"}})


@pytest.fixture
async def alice(tenants, owner_engine):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": ALICE}
            )
        ).scalar_one()


def _capture(
    user_id,
    *,
    at=T0,
    where=COVE,
    install="install-1",
    place="La Jolla Cove",
    manifest=None,
):
    client_uuid = uuid.uuid4()
    digest = client_uuid.hex[:8]
    return MobileCapture(
        client_capture_uuid=client_uuid,
        uploaded_by=user_id,
        captured_at=at,
        device_install_id=install,
        device_model="iPhone16,1",
        device_info=None,
        app_version="1.1.0+12",
        core_version=None,
        model_version=None,
        latitude=None if where is None else where[0],
        longitude=None if where is None else where[1],
        horizontal_accuracy_m=None if where is None else 4.5,
        place_name=place,
        orientation="portrait",
        intrinsics=(1400.0, 0.0, 960.0, 0.0, 1400.0, 720.0, 0.0, 0.0, 1.0),
        camera_pose=None,
        depth_width=256,
        depth_height=192,
        depth_row_bytes=1024,
        confidence_width=256,
        confidence_height=192,
        mask_width=None,
        mask_height=None,
        files=tuple(
            CaptureFile(
                role=role,
                bucket="fishsense-tenant-lab",
                key=f"mobile/captures/{client_uuid}/{role}",
                sha256=(digest * 8)[:64] if role == "rgb_jpeg" else c * 64,
                size=1000,
                content_type="application/octet-stream",
            )
            for role, c in (
                ("rgb_jpeg", "a"),
                ("depth_f32", "b"),
                ("confidence_u8", "c"),
            )
        ),
        manifest=manifest or {"as": "received"},
    )


async def _commit(app_engine, tenant_id, capture):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await commit_capture(conn, tenant_id, capture, [])


async def _mission_of(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("""
                    SELECT m.id, m.number, m.name, m.source, m.source_path,
                           m.started_at, m.device_id = c.device_id AS same_device
                    FROM captures c JOIN missions m ON m.id = c.mission_id
                    WHERE c.id = :id
                    """),
                {"id": capture_id},
            )
        ).one_or_none()


async def _missions(owner_engine) -> int:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM missions"))).scalar_one()


async def test_a_first_capture_starts_a_phone_mission(
    tenants, alice, app_engine, owner_engine
):
    committed = await _commit(app_engine, tenants["lab"], _capture(alice))

    mission = await _mission_of(owner_engine, committed.capture_id)
    assert mission is not None
    assert (mission.source, mission.source_path) == ("mobile", None)
    assert mission.started_at == T0
    assert mission.same_device
    assert mission.name == "2026.10.11.FishSense.La Jolla Cove"


async def test_a_capture_within_two_hours_and_a_kilometre_joins_it(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit(app_engine, tenants["lab"], _capture(alice))
    second = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=T0 + timedelta(hours=2), where=NEAR),
    )

    assert (await _mission_of(owner_engine, second.capture_id)).id == (
        await _mission_of(owner_engine, first.capture_id)
    ).id


async def test_more_than_two_hours_later_starts_another(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit(app_engine, tenants["lab"], _capture(alice))
    second = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=T0 + timedelta(hours=2, seconds=1)),
    )

    assert (await _mission_of(owner_engine, second.capture_id)).id != (
        await _mission_of(owner_engine, first.capture_id)
    ).id


async def test_more_than_a_kilometre_away_starts_another(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit(app_engine, tenants["lab"], _capture(alice))
    second = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=T0 + timedelta(minutes=10), where=FAR),
    )

    assert (await _mission_of(owner_engine, second.capture_id)).id != (
        await _mission_of(owner_engine, first.capture_id)
    ).id


async def test_without_a_location_time_alone_decides(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit(app_engine, tenants["lab"], _capture(alice))
    second = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=T0 + timedelta(minutes=30), where=None),
    )

    assert (await _mission_of(owner_engine, second.capture_id)).id == (
        await _mission_of(owner_engine, first.capture_id)
    ).id


async def test_another_phone_has_its_own_missions(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit(app_engine, tenants["lab"], _capture(alice))
    other = await _commit(
        app_engine, tenants["lab"], _capture(alice, install="install-2")
    )

    assert (await _mission_of(owner_engine, other.capture_id)).id != (
        await _mission_of(owner_engine, first.capture_id)
    ).id


async def test_a_late_sync_from_earlier_joins_and_moves_the_start(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit(app_engine, tenants["lab"], _capture(alice))
    earlier = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=T0 - timedelta(minutes=45)),
    )

    mission = await _mission_of(owner_engine, earlier.capture_id)
    assert mission.id == (await _mission_of(owner_engine, first.capture_id)).id
    assert mission.started_at == T0 - timedelta(minutes=45)


async def test_a_capture_between_two_missions_joins_the_nearer_and_merges_nothing(
    tenants, alice, app_engine, owner_engine
):
    morning = await _commit(app_engine, tenants["lab"], _capture(alice))
    evening = await _commit(
        app_engine, tenants["lab"], _capture(alice, at=T0 + timedelta(hours=3))
    )
    between = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=T0 + timedelta(hours=1, minutes=50)),
    )

    assert (await _mission_of(owner_engine, between.capture_id)).id == (
        await _mission_of(owner_engine, evening.capture_id)
    ).id
    assert (await _mission_of(owner_engine, morning.capture_id)).id != (
        await _mission_of(owner_engine, evening.capture_id)
    ).id


async def test_the_name_takes_the_phones_local_day(
    tenants, alice, app_engine, owner_engine
):
    """03:30 UTC on the 12th is the evening of the 11th in San Diego."""
    committed = await _commit(
        app_engine,
        tenants["lab"],
        _capture(
            alice,
            at=datetime(2026, 10, 12, 3, 30, tzinfo=UTC),
            manifest={"time_zone": "America/Los_Angeles"},
        ),
    )

    mission = await _mission_of(owner_engine, committed.capture_id)
    assert mission.name == "2026.10.11.FishSense.La Jolla Cove"


async def test_without_a_time_zone_the_day_is_utcs(
    tenants, alice, app_engine, owner_engine
):
    committed = await _commit(
        app_engine,
        tenants["lab"],
        _capture(alice, at=datetime(2026, 10, 12, 3, 30, tzinfo=UTC)),
    )

    assert (await _mission_of(owner_engine, committed.capture_id)).name == (
        "2026.10.12.FishSense.La Jolla Cove"
    )


async def test_an_unknown_time_zone_falls_back_to_utc(
    tenants, alice, app_engine, owner_engine
):
    committed = await _commit(
        app_engine,
        tenants["lab"],
        _capture(
            alice,
            at=datetime(2026, 10, 12, 3, 30, tzinfo=UTC),
            manifest={"time_zone": "Mars/Olympus_Mons"},
        ),
    )

    assert (await _mission_of(owner_engine, committed.capture_id)).name.startswith(
        "2026.10.12."
    )


async def test_without_a_place_the_name_says_mobile(
    tenants, alice, app_engine, owner_engine
):
    committed = await _commit(app_engine, tenants["lab"], _capture(alice, place=None))

    assert (await _mission_of(owner_engine, committed.capture_id)).name == (
        "2026.10.11.FishSense.Mobile"
    )


async def test_a_retried_commit_makes_no_second_mission(
    tenants, alice, app_engine, owner_engine
):
    capture = _capture(alice)
    await _commit(app_engine, tenants["lab"], capture)
    await _commit(app_engine, tenants["lab"], capture)

    assert await _missions(owner_engine) == 1


async def test_a_lite_mission_must_have_its_folder_and_a_phone_one_none(
    tenants, owner_engine
):
    for source, path in (("lite", None), ("mobile", "some/folder")):
        with pytest.raises(IntegrityError):
            async with owner_engine.begin() as conn:
                await conn.execute(
                    text("""
                        INSERT INTO missions (tenant_id, source, source_path, started_at)
                        VALUES (:t, :s, :p, now())
                        """),
                    {"t": tenants["lab"], "s": source, "p": path},
                )


async def test_a_mission_is_lite_unless_it_says_otherwise(tenants, owner_engine):
    async with owner_engine.begin() as conn:
        source = (
            await conn.execute(
                text("""
                    INSERT INTO missions (tenant_id, source_path, started_at)
                    VALUES (:t, 'a/folder', now()) RETURNING source
                    """),
                {"t": tenants["lab"]},
            )
        ).scalar_one()

    assert source == "lite"
