"""Laser calibrations: per dive, append-only; refusals are rows (PLAN.md §9.13).

v1 overwrote one extrinsics row per dive and kept refusals as dive columns.
Here every attempt is appended -- accepted with a laser position and axis, or
refused with a reason -- and ``current`` is the latest per dive. The
*effective* calibration a dive is measured with follows v1's borrowing: a dive
that names a calibration source (same tenant only, §9.17) uses that dive's
current calibration, and only an accepted one.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction

POSITION, AXIS = [0.104, 0.0, 0.0], [0.0, 0.0, 1.0]


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant(conn, slug: str) -> uuid.UUID:
    return await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )


async def _dive(conn, tenant, path: str, source=None) -> uuid.UUID:
    return await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at, "
        "calibration_source_dive_id) VALUES (:t, :p, now(), :src) RETURNING id",
        t=tenant,
        p=path,
        src=source,
    )


async def _accepted(conn, tenant, dive, **columns) -> uuid.UUID:
    return await _calibration(
        conn,
        tenant,
        dive,
        outcome="accepted",
        producer="slate",
        laser_position=json.dumps(POSITION),
        laser_axis=json.dumps(AXIS),
        **columns,
    )


async def _refused(conn, tenant, dive, reason="baseline implausible") -> uuid.UUID:
    return await _calibration(
        conn, tenant, dive, outcome="refused", producer="slate", refusal_reason=reason
    )


async def _calibration(conn, tenant, dive, **columns) -> uuid.UUID:
    names = ", ".join(["tenant_id", "dive_id", *columns])
    params = ", ".join([":tenant_id", ":dive_id", *(f":{c}" for c in columns)])
    return await _one(
        conn,
        f"INSERT INTO laser_calibrations ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        dive_id=dive,
        **columns,
    )


async def _effective(conn, dive) -> uuid.UUID | None:
    return (
        await conn.execute(
            text(
                "SELECT laser_calibration_id FROM effective_laser_calibrations "
                "WHERE dive_id = :d"
            ),
            {"d": dive},
        )
    ).scalar_one_or_none()


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


# --- append-only, and each outcome carries what it must -------------------------


async def test_the_app_role_appends_but_never_rewrites_a_calibration(
    owner_engine, app_engine
):
    async with owner_engine.begin() as conn:
        lab = await _tenant(conn, "lab")
        dive = await _dive(conn, lab, "/a")

    async with tenant_transaction(app_engine, lab) as conn:
        await _accepted(conn, lab, dive)

    for statement in (
        "UPDATE laser_calibrations SET outcome = 'refused'",
        "DELETE FROM laser_calibrations",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, lab) as conn:
                await conn.execute(text(statement))


async def test_an_accepted_calibration_carries_its_laser_geometry(owner):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/a")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _calibration(owner, lab, dive, outcome="accepted", producer="slate")


async def test_a_refusal_carries_its_reason(owner):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/a")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _calibration(owner, lab, dive, outcome="refused", producer="slate")


async def test_only_a_migrated_row_may_have_an_unknown_producer(owner):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/a")
    geometry = {
        "outcome": "accepted",
        "laser_position": json.dumps(POSITION),
        "laser_axis": json.dumps(AXIS),
    }

    await _calibration(owner, lab, dive, v1_id=42, **geometry)
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _calibration(owner, lab, dive, **geometry)


# --- current and effective --------------------------------------------------------


async def test_a_later_refusal_leaves_the_dive_without_an_effective_calibration(
    owner,
):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/a")
    accepted = await _accepted(owner, lab, dive)
    assert await _effective(owner, dive) == accepted

    await _refused(owner, lab, dive)

    assert await _effective(owner, dive) is None
    assert (
        await _one(
            owner,
            "SELECT outcome FROM current_laser_calibrations WHERE dive_id = :d",
            d=dive,
        )
        == "refused"
    )


async def test_a_dive_that_borrows_is_measured_with_its_sources_calibration(owner):
    lab = await _tenant(owner, "lab")
    source = await _dive(owner, lab, "/source")
    borrower = await _dive(owner, lab, "/borrower", source=source)
    sources = await _accepted(owner, lab, source)
    await _accepted(owner, lab, borrower)  # its own is ignored while it borrows

    assert await _effective(owner, borrower) == sources


async def test_effective_calibrations_are_visible_only_within_the_tenant(
    owner_engine, app_engine
):
    async with owner_engine.begin() as conn:
        lab, partner = await _tenant(conn, "lab"), await _tenant(conn, "partner")
        await _accepted(conn, lab, await _dive(conn, lab, "/l"))
        await _accepted(conn, partner, await _dive(conn, partner, "/p"))

    async with tenant_transaction(app_engine, lab) as conn:
        count = await _one(conn, "SELECT count(*) FROM effective_laser_calibrations")

    assert count == 1
