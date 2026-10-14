"""A FishSense Mobile capture, committed: everything the app saved, kept.

PLAN.md §9.6: captures go mobile -> server as append-only, immutable facts,
idempotent on the client's UUID, bound to the user who captured them. This is
scientific data, so a capture keeps everything the app saved -- every file
(RGB, the lossless frame when the app sends one, depth, confidence, mask) with
its checksum, every number (intrinsics, pose, depth stride, location, device,
app / fishsense-core / model versions), and the request as received -- so a
measurement can be reproduced from what the server holds.

A redone two-point selection on the phone is a new append-only measurement
version (§9.6), never an edit; current is the latest per (capture, fish,
source) (§9.13).
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import (
    CaptureConflict,
    CaptureFile,
    DeviceMeasurement,
    MobileCapture,
    NoSuchCapture,
    append_device_measurement,
    commit_capture,
    list_mobile_captures,
    mobile_capture,
)

ALICE = "sub-alice"
BOB = "sub-bob"
T0 = datetime(2026, 10, 11, 17, 30, tzinfo=UTC)


async def _user_id(owner_engine, sub: str) -> uuid.UUID:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT id FROM users WHERE sub = :s"), {"s": sub})
        ).scalar_one()


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships(
        {ALICE: {"lab": "member", "partner": "member"}, BOB: {"lab": "member"}}
    )


def _file(role: str, digest: str, *, bucket="fishsense-tenant-lab") -> CaptureFile:
    return CaptureFile(
        role=role,
        bucket=bucket,
        key=f"mobile/captures/{digest}/{role}",
        sha256=digest * 64,
        size=1000,
        content_type="application/octet-stream",
    )


def _measurement(*, origin="auto", length=0.42, at=T0, key=None) -> DeviceMeasurement:
    return DeviceMeasurement(
        client_measurement_uuid=key or uuid.uuid4(),
        measured_at=at,
        length_m=length,
        snout=(100.0, 200.0),
        fork=(900.0, 210.0),
        origin=origin,
        algorithm="fishsense-mobile.compute_length",
        algorithm_version="1.0.0",
        core_version="fishsense_core-v2.0.0",
        model_version="fishial@unpinned",
    )


def _capture(
    user_id, *, client_uuid=None, files=None, at=T0, install="install-1", manifest=None
):
    client_uuid = client_uuid or uuid.uuid4()
    return MobileCapture(
        client_capture_uuid=client_uuid,
        uploaded_by=user_id,
        captured_at=at,
        device_install_id=install,
        device_model="iPhone16,1",
        device_info="Apple iPhone (iOS 17.5 - LiDAR Enabled)",
        app_version="1.1.0+12",
        core_version="fishsense_core-v2.0.0",
        model_version="fishial@unpinned",
        latitude=32.8801,
        longitude=-117.2340,
        horizontal_accuracy_m=4.5,
        place_name="La Jolla Cove",
        orientation="portrait",
        intrinsics=(1400.0, 0.0, 960.0, 0.0, 1400.0, 720.0, 0.0, 0.0, 1.0),
        camera_pose=None,
        depth_width=256,
        depth_height=192,
        depth_row_bytes=1024,
        confidence_width=256,
        confidence_height=192,
        mask_width=1920,
        mask_height=1440,
        files=tuple(
            replace(f, key=f"mobile/captures/{client_uuid}/{f.role}")
            for f in files
            or (
                _file("rgb_jpeg", "a"),
                _file("depth_f32", "b"),
                _file("confidence_u8", "c"),
                _file("mask_u8", "d"),
            )
        ),
        manifest=manifest or {"as": "received", "unmodeled_field": 1},
    )


async def _commit(app_engine, tenant_id, capture, measurements=()):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await commit_capture(conn, tenant_id, capture, list(measurements))


async def test_a_commit_keeps_the_capture_its_files_and_its_measurement(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)

    committed = await _commit(app_engine, tenants["lab"], capture, [_measurement()])

    assert committed.created
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        found = await mobile_capture(conn, tenants["lab"], capture.client_capture_uuid)
    assert found.capture_id == committed.capture_id
    assert found.captured_at == T0
    assert found.uploaded_by == alice
    assert found.intrinsics == capture.intrinsics
    assert found.depth_row_bytes == 1024
    assert found.manifest == {"as": "received", "unmodeled_field": 1}
    assert {f.role for f in found.files} == {
        "rgb_jpeg",
        "depth_f32",
        "confidence_u8",
        "mask_u8",
    }
    assert [m.length_m for m in found.measurements] == [0.42]
    assert found.current_measurement.origin == "auto"


async def test_the_capture_row_names_its_rgb_by_checksum_and_device(
    tenants, app_engine, owner_engine
):
    capture = _capture(await _user_id(owner_engine, ALICE))
    committed = await _commit(app_engine, tenants["lab"], capture)

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT c.checksum, c.checksum_algorithm, c.raw_object_key, "
                    "c.is_canonical, c.dive_id, d.kind, d.serial "
                    "FROM captures c JOIN devices d ON d.id = c.device_id "
                    "WHERE c.id = :id"
                ),
                {"id": committed.capture_id},
            )
        ).one()
    assert row.checksum == "a" * 64
    assert row.checksum_algorithm == "sha256"
    assert (
        row.raw_object_key == f"mobile/captures/{capture.client_capture_uuid}/rgb_jpeg"
    )
    assert row.is_canonical and row.dive_id is None
    assert (row.kind, row.serial) == ("mobile", "install-1")


async def test_committing_again_is_a_no_op(tenants, app_engine, owner_engine):
    capture = _capture(await _user_id(owner_engine, ALICE))
    measured = _measurement()
    first = await _commit(app_engine, tenants["lab"], capture, [measured])

    again = await _commit(app_engine, tenants["lab"], capture, [measured])

    assert not again.created
    assert again.capture_id == first.capture_id
    async with owner_engine.connect() as conn:
        counts = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM captures), "
                    "(SELECT count(*) FROM measurements), (SELECT count(*) FROM fish)"
                )
            )
        ).one()
    assert tuple(counts) == (1, 1, 1)


async def test_another_user_cannot_commit_a_capture_under_the_same_uuid(
    tenants, app_engine, owner_engine
):
    """Captures are bound to the user who took them (§9.6)."""
    capture = _capture(await _user_id(owner_engine, ALICE))
    await _commit(app_engine, tenants["lab"], capture)
    bobs = _capture(
        await _user_id(owner_engine, BOB), client_uuid=capture.client_capture_uuid
    )

    with pytest.raises(CaptureConflict):
        await _commit(app_engine, tenants["lab"], bobs)


async def test_a_committed_capture_is_immutable(tenants, app_engine, owner_engine):
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)
    await _commit(app_engine, tenants["lab"], capture)
    changed = _capture(
        alice,
        client_uuid=capture.client_capture_uuid,
        files=(
            _file("rgb_jpeg", "e"),
            _file("depth_f32", "b"),
            _file("confidence_u8", "c"),
        ),
    )

    with pytest.raises(CaptureConflict):
        await _commit(app_engine, tenants["lab"], changed)


async def test_one_phone_is_one_device(tenants, app_engine, owner_engine):
    alice = await _user_id(owner_engine, ALICE)
    for digest in ("1", "2"):
        files = (
            _file("rgb_jpeg", digest),
            _file("depth_f32", "b"),
            _file("confidence_u8", "c"),
        )
        await _commit(app_engine, tenants["lab"], _capture(alice, files=files))

    async with owner_engine.connect() as conn:
        devices = (
            await conn.execute(
                text("SELECT count(*) FROM devices WHERE kind = 'mobile'")
            )
        ).scalar_one()
    assert devices == 1


async def test_a_redone_selection_is_a_new_version_and_becomes_current(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)
    await _commit(app_engine, tenants["lab"], capture, [_measurement()])
    redone = _measurement(origin="manual", length=0.45, at=T0 + timedelta(minutes=3))

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        appended = await append_device_measurement(
            conn, tenants["lab"], capture.client_capture_uuid, alice, redone
        )
        found = await mobile_capture(conn, tenants["lab"], capture.client_capture_uuid)

    assert appended.created
    assert appended.measurement.length_m == 0.45
    assert [(m.origin, m.length_m) for m in found.measurements] == [
        ("auto", 0.42),
        ("manual", 0.45),
    ]
    assert found.current_measurement.length_m == 0.45


async def test_appending_the_same_version_again_is_a_no_op(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)
    await _commit(app_engine, tenants["lab"], capture)
    redone = _measurement(origin="manual")

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        first = await append_device_measurement(
            conn, tenants["lab"], capture.client_capture_uuid, alice, redone
        )
        again = await append_device_measurement(
            conn, tenants["lab"], capture.client_capture_uuid, alice, redone
        )
        found = await mobile_capture(conn, tenants["lab"], capture.client_capture_uuid)

    assert (first.created, again.created) == (True, False)
    assert len(found.measurements) == 1


async def test_only_the_capturer_may_add_a_version(tenants, app_engine, owner_engine):
    capture = _capture(await _user_id(owner_engine, ALICE))
    await _commit(app_engine, tenants["lab"], capture)

    with pytest.raises(CaptureConflict):
        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            await append_device_measurement(
                conn,
                tenants["lab"],
                capture.client_capture_uuid,
                await _user_id(owner_engine, BOB),
                _measurement(origin="manual"),
            )


async def test_a_version_for_an_unknown_capture_is_refused(
    tenants, app_engine, owner_engine
):
    with pytest.raises(NoSuchCapture):
        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            await append_device_measurement(
                conn,
                tenants["lab"],
                uuid.uuid4(),
                await _user_id(owner_engine, ALICE),
                _measurement(),
            )


async def test_captures_list_newest_first_with_their_current_length(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    older = _capture(
        alice,
        at=T0,
        files=(
            _file("rgb_jpeg", "1"),
            _file("depth_f32", "b"),
            _file("confidence_u8", "c"),
        ),
    )
    newer = _capture(
        alice,
        at=T0 + timedelta(hours=1),
        files=(
            _file("rgb_jpeg", "2"),
            _file("depth_f32", "b"),
            _file("confidence_u8", "c"),
        ),
    )
    await _commit(app_engine, tenants["lab"], older, [_measurement(length=0.3)])
    await _commit(app_engine, tenants["lab"], newer)

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        listed = await list_mobile_captures(conn, tenants["lab"], limit=10)

    assert [c.client_capture_uuid for c in listed] == [
        newer.client_capture_uuid,
        older.client_capture_uuid,
    ]
    assert [c.length_m for c in listed] == [None, 0.3]
    assert listed[1].rgb_key == f"mobile/captures/{older.client_capture_uuid}/rgb_jpeg"


async def test_listing_pages_by_time(tenants, app_engine, owner_engine):
    alice = await _user_id(owner_engine, ALICE)
    for hour in range(3):
        files = (
            _file("rgb_jpeg", str(hour)),
            _file("depth_f32", "b"),
            _file("confidence_u8", "c"),
        )
        await _commit(
            app_engine,
            tenants["lab"],
            _capture(alice, at=T0 + timedelta(hours=hour), files=files),
        )

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        first = await list_mobile_captures(conn, tenants["lab"], limit=2)
        rest = await list_mobile_captures(
            conn, tenants["lab"], limit=2, before=first[-1].cursor
        )

    assert [c.captured_at for c in first + rest] == [
        T0 + timedelta(hours=2),
        T0 + timedelta(hours=1),
        T0,
    ]


async def test_another_tenant_sees_none_of_them(tenants, app_engine, owner_engine):
    capture = _capture(await _user_id(owner_engine, ALICE))
    await _commit(app_engine, tenants["lab"], capture)

    async with tenant_transaction(app_engine, tenants["partner"]) as conn:
        assert await list_mobile_captures(conn, tenants["partner"], limit=10) == []
        assert (
            await mobile_capture(conn, tenants["partner"], capture.client_capture_uuid)
            is None
        )


# -- code review of #63 ---------------------------------------------------------------


def _rgb_only(digest: str):
    return (
        _file("rgb_jpeg", digest),
        _file("depth_f32", "b"),
        _file("confidence_u8", "c"),
    )


async def test_the_same_photo_under_another_uuid_names_the_first_capture(
    tenants, app_engine, owner_engine
):
    """A byte-identical photo is one capture: a second uuid for it is a
    conflict naming the first, not a 500 the phone retries forever."""
    alice = await _user_id(owner_engine, ALICE)
    first = _capture(alice, files=_rgb_only("7"))
    await _commit(app_engine, tenants["lab"], first)

    with pytest.raises(CaptureConflict, match=str(first.client_capture_uuid)):
        await _commit(app_engine, tenants["lab"], _capture(alice, files=_rgb_only("7")))


async def test_concurrent_commits_of_one_capture_both_succeed(
    tenants, app_engine, owner_engine
):
    """A phone's retry can race its own first attempt: one keeps the
    capture, the other finds it kept -- neither fails."""
    alice = await _user_id(owner_engine, ALICE)
    for digest in "89":
        capture = _capture(alice, files=_rgb_only(digest))
        version = _measurement()
        results = await asyncio.gather(
            _commit(app_engine, tenants["lab"], capture, [version]),
            _commit(app_engine, tenants["lab"], capture, [version]),
        )
        assert sorted(r.created for r in results) == [False, True]

    async with owner_engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM measurements"))
        ).scalar_one() == 2


async def test_concurrent_appends_of_one_version_both_succeed(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)
    await _commit(app_engine, tenants["lab"], capture)
    redone = _measurement(origin="manual")

    async def append():
        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            return await append_device_measurement(
                conn, tenants["lab"], capture.client_capture_uuid, alice, redone
            )

    results = await asyncio.gather(append(), append())

    assert sorted(r.created for r in results) == [False, True]


async def test_a_changed_capture_under_a_committed_uuid_is_a_conflict(
    tenants, app_engine, owner_engine
):
    """A committed capture is immutable: the same files with other metadata
    (a corrected intrinsics, say) is refused, not silently ignored."""
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice, manifest={"intrinsics": [1400.0]})
    await _commit(app_engine, tenants["lab"], capture)
    corrected = _capture(
        alice,
        client_uuid=capture.client_capture_uuid,
        manifest={"intrinsics": [1500.0]},
    )

    with pytest.raises(CaptureConflict, match="immutable"):
        await _commit(app_engine, tenants["lab"], corrected)


async def test_a_retry_carrying_more_versions_is_not_a_change(
    tenants, app_engine, owner_engine
):
    """The versions travel in the request but are appended, not compared: a
    retry after a redone selection is the same capture."""
    alice = await _user_id(owner_engine, ALICE)
    first = _measurement()
    capture = _capture(alice, manifest={"x": 1, "measurements": ["v1"]})
    await _commit(app_engine, tenants["lab"], capture, [first])
    retry = _capture(
        alice,
        client_uuid=capture.client_capture_uuid,
        manifest={"x": 1, "measurements": ["v1", "v2"]},
    )

    again = await _commit(
        app_engine,
        tenants["lab"],
        retry,
        [first, _measurement(origin="manual", at=T0 + timedelta(minutes=1))],
    )

    assert not again.created


async def test_a_measurement_uuid_belongs_to_one_capture(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    a = _capture(alice, files=_rgb_only("a"))
    b = _capture(alice, files=_rgb_only("b"))
    version = _measurement()
    await _commit(app_engine, tenants["lab"], a, [version])
    await _commit(app_engine, tenants["lab"], b)

    with pytest.raises(CaptureConflict):
        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            await append_device_measurement(
                conn, tenants["lab"], b.client_capture_uuid, alice, version
            )


async def test_a_measurement_uuid_resent_with_other_values_is_a_conflict(
    tenants, app_engine, owner_engine
):
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)
    version = _measurement()
    await _commit(app_engine, tenants["lab"], capture, [version])

    with pytest.raises(CaptureConflict):
        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            await append_device_measurement(
                conn,
                tenants["lab"],
                capture.client_capture_uuid,
                alice,
                replace(version, length_m=0.99),
            )


async def test_current_is_the_latest_measured_not_the_latest_received(
    tenants, app_engine, owner_engine
):
    """A version that arrives late (a stale retry) doesn't displace a newer
    one: everywhere "current" is read -- the capture, the list, and the
    shared `current_measurements` view (§9.13)."""
    alice = await _user_id(owner_engine, ALICE)
    capture = _capture(alice)
    newer = _measurement(origin="manual", length=0.45, at=T0 + timedelta(minutes=5))
    older = _measurement(origin="auto", length=0.42, at=T0)
    await _commit(app_engine, tenants["lab"], capture, [newer])

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        await append_device_measurement(
            conn, tenants["lab"], capture.client_capture_uuid, alice, older
        )
        found = await mobile_capture(conn, tenants["lab"], capture.client_capture_uuid)
        listed = await list_mobile_captures(conn, tenants["lab"], limit=10)
        view = (
            await conn.execute(
                text(
                    "SELECT length_m FROM current_measurements "
                    "WHERE capture_id = :id AND source = 'device'"
                ),
                {"id": found.capture_id},
            )
        ).scalar_one()

    assert [m.length_m for m in found.measurements] == [0.42, 0.45]
    assert found.current_measurement.length_m == 0.45
    assert listed[0].length_m == 0.45
    assert view == 0.45
