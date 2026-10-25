"""The database side of the focal-length backfill: each NAS frame's EXIF focal
length, recorded once.

New in v2 (2026-10-25). The TG-6 zooms 4.5-18 mm and its intrinsics are
calibrated at 4.5; missions 374, 384 and 386 mix 4.5 with up to 18 mm, and a
zoomed frame's laser dots sit on another line. Ingest now keeps the focal
length (0x920A); the ~131k frames ingested before it are read back from the
NAS (4 KB each) by the orchestrator (``ops.focal_lengths``). This store is
what it reads and the one write it makes.

Pinned here:

* a mission's frames still to read: a NAS path, no focal length, path order
  (a frame held only in the object store, or already read, is not listed);
* the missions with frames still to read, with how many;
* the write fills an unknown focal length only -- never overwrites one -- and
  says whether it did;
* everything is per tenant, and the catalog acts only as a member.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.focal_length_store import (
    FocalLengthCatalog,
    FocalLengthFrame,
    frames_without_focal_length,
    missions_without_focal_lengths,
    record_focal_length,
)
from fishsense_services_api.service_principal import NotAMember

T0 = datetime(2024, 6, 20, 10, 27, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"
DIVE = "2024.06.20.REEF/062024_Alligator Anchor_FSL04"


def _md5(n: int) -> str:
    return f"{n:032x}"


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _dive(owner_engine, tenant, path=DIVE) -> tuple[uuid.UUID, int]:
    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO missions (tenant_id, source_path, started_at) "
                    "VALUES (:t, :p, :at) RETURNING id, number"
                ),
                {"t": tenant, "p": path, "at": T0},
            )
        ).one()
    return row.id, row.number


async def _capture(owner_engine, tenant, mission, n, *, path=None, focal=None):
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO captures (tenant_id, mission_id, source_path, "
                    "raw_object_key, captured_at, checksum, is_canonical, "
                    "focal_length_mm) "
                    "VALUES (:t, :d, :path, :key, :at, :sum, true, :focal) "
                    "RETURNING id"
                ),
                {"t": tenant, "d": mission, "path": path,
                 "key": None if path else f"uploads/{_md5(n)}.jpg", "at": T0,
                 "sum": _md5(n), "focal": focal},
            )
        ).scalar_one()  # fmt: skip


async def _focal(owner_engine, capture_id) -> float | None:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("SELECT focal_length_mm FROM captures WHERE id = :c"),
                {"c": capture_id},
            )
        ).scalar_one()


async def test_a_missions_frames_still_to_read_in_path_order(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    mission, _ = await _dive(owner_engine, lab)
    second = await _capture(owner_engine, lab, mission, 2, path=f"{DIVE}/P2.ORF")
    first = await _capture(owner_engine, lab, mission, 1, path=f"{DIVE}/P1.ORF")
    await _capture(owner_engine, lab, mission, 3, path=f"{DIVE}/P3.ORF", focal=4.5)
    await _capture(owner_engine, lab, mission, 4)  # object store only: a phone

    async with tenant_transaction(app_engine, lab) as conn:
        frames = await frames_without_focal_length(conn, lab, mission)

    assert frames == [
        FocalLengthFrame(first, f"{DIVE}/P1.ORF"),
        FocalLengthFrame(second, f"{DIVE}/P2.ORF"),
    ]


async def test_the_missions_with_frames_still_to_read(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    todo, todo_number = await _dive(owner_engine, lab)
    done, _ = await _dive(owner_engine, lab, path=f"{DIVE}_done")
    for n in (1, 2):
        await _capture(owner_engine, lab, todo, n, path=f"{DIVE}/P{n}.ORF")
    await _capture(owner_engine, lab, done, 3, path=f"{DIVE}_done/P3.ORF", focal=4.5)

    async with tenant_transaction(app_engine, lab) as conn:
        missions = await missions_without_focal_lengths(conn, lab)

    assert [(m.id, m.number, m.frames) for m in missions] == [(todo, todo_number, 2)]


async def test_a_focal_length_is_recorded_once_and_never_overwritten(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    mission, _ = await _dive(owner_engine, lab)
    frame = await _capture(owner_engine, lab, mission, 1, path=f"{DIVE}/P1.ORF")

    async with tenant_transaction(app_engine, lab) as conn:
        first = await record_focal_length(conn, lab, frame, 14.62)
    async with tenant_transaction(app_engine, lab) as conn:
        second = await record_focal_length(conn, lab, frame, 4.5)

    assert (first, second) == (True, False)
    assert await _focal(owner_engine, frame) == pytest.approx(14.62)


async def test_a_focal_length_must_be_positive(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    mission, _ = await _dive(owner_engine, lab)
    frame = await _capture(owner_engine, lab, mission, 1, path=f"{DIVE}/P1.ORF")

    with pytest.raises(Exception, match="focal_length_mm"):
        async with tenant_transaction(app_engine, lab) as conn:
            await record_focal_length(conn, lab, frame, 0.0)


async def test_another_tenants_frame_cannot_be_written(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    mission, _ = await _dive(owner_engine, partner)
    frame = await _capture(owner_engine, partner, mission, 1, path=f"{DIVE}/P1.ORF")

    async with tenant_transaction(app_engine, lab) as conn:
        assert await record_focal_length(conn, lab, frame, 4.5) is False
        assert await frames_without_focal_length(conn, lab, mission) == []
    assert await _focal(owner_engine, frame) is None


async def test_the_catalog_backfills_within_a_tenant_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    mission, number = await _dive(owner_engine, lab)
    frame = await _capture(owner_engine, lab, mission, 1, path=f"{DIVE}/P1.ORF")
    catalog = FocalLengthCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert [m.number for m in await catalog.missions_without_focal_lengths(lab)] == [
        number
    ]
    assert await catalog.frames_without_focal_length(lab, mission) == [
        FocalLengthFrame(frame, f"{DIVE}/P1.ORF")
    ]
    assert await catalog.record_focal_length(lab, frame, 4.5)
    assert await _focal(owner_engine, frame) == pytest.approx(4.5)


async def test_the_catalog_refuses_a_tenant_it_is_not_a_member_of(
    app_engine, seed_memberships
):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = FocalLengthCatalog(app_engine, sub=ORCHESTRATOR)

    with pytest.raises(NotAMember):
        await catalog.record_focal_length(tenants["partner"], uuid.uuid4(), 4.5)
