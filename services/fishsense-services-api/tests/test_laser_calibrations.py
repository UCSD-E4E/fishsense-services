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
    columns = {
        "laser_position": json.dumps(POSITION),
        "laser_axis": json.dumps(AXIS),
        **columns,
    }
    return await _calibration(
        conn, tenant, dive, outcome="accepted", producer="slate", **columns
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


# --- what a dive is measured with: v1's rule ----------------------------------------
#
# fishsense-lite@a8b2c3bc dive_controller.get_laser_extrinsics_for_dive: a dive's
# *own* calibration wins; only if it has none does it borrow its link's. And
# `_plausible_extrinsics`: a stored fit whose baseline is outside 0.097-0.145 m
# (norm of laser_position's x and y) counts as no calibration, everywhere --
# eight of v1's 35 stored fits were 2.35-22.22 cm against a fleet IQR of
# 9.99-10.45 cm, backing 663 of 3,104 measurements at -75% to +45% error. A
# borrowed fit gets the same test: dive 518's 2.60 cm fit, borrowed by two
# others, made three dives of wrong lengths. v2 kept its refusal rule (a later
# refusal is the dive's current calibration, so it has none of its own).


async def _effective_row(conn, dive):
    return (
        await conn.execute(
            text(
                "SELECT laser_calibration_id, source_dive_id, borrowed "
                "FROM effective_laser_calibrations WHERE dive_id = :d"
            ),
            {"d": dive},
        )
    ).one_or_none()


async def test_a_dives_own_calibration_wins_over_its_link(owner):
    """Migration 0008 had this backwards (the link won) while saying it
    followed v1."""
    lab = await _tenant(owner, "lab")
    source = await _dive(owner, lab, "/source")
    dive = await _dive(owner, lab, "/dive", source=source)
    await _accepted(owner, lab, source)
    own = await _accepted(owner, lab, dive)

    assert tuple(await _effective_row(owner, dive)) == (own, dive, False)


async def test_a_dive_without_its_own_borrows_its_links(owner):
    lab = await _tenant(owner, "lab")
    source = await _dive(owner, lab, "/source")
    dive = await _dive(owner, lab, "/dive", source=source)
    sources = await _accepted(owner, lab, source)

    assert tuple(await _effective_row(owner, dive)) == (sources, source, True)


async def test_a_refused_own_calibration_falls_back_to_the_link(owner):
    lab = await _tenant(owner, "lab")
    source = await _dive(owner, lab, "/source")
    dive = await _dive(owner, lab, "/dive", source=source)
    sources = await _accepted(owner, lab, source)
    await _accepted(owner, lab, dive)
    await _refused(owner, lab, dive)

    assert await _effective(owner, dive) == sources


@pytest.mark.parametrize(
    "position, plausible",
    [
        ([0.097, 0.0, 0.0], True),
        ([0.145, 0.0, 0.0], True),
        ([0.06, 0.08, 0.0], True),  # the norm of x and y: 0.10
        ([0.0969, 0.0, 0.0], False),
        ([0.1451, 0.0, 0.0], False),
        ([0.0235, 0.0, 0.0], False),
        ([0.104, 0.0, 5.0], True),  # z carries nothing (both producers pad it)
    ],
)
async def test_an_implausible_baseline_counts_as_no_calibration(
    owner, position, plausible
):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/dive")
    fit = await _accepted(owner, lab, dive, laser_position=json.dumps(position))

    assert (await _effective(owner, dive) == fit) is plausible


async def test_an_implausible_own_calibration_falls_back_to_the_link(owner):
    lab = await _tenant(owner, "lab")
    source = await _dive(owner, lab, "/source")
    dive = await _dive(owner, lab, "/dive", source=source)
    sources = await _accepted(owner, lab, source)
    await _accepted(owner, lab, dive, laser_position=json.dumps([0.0235, 0, 0]))

    assert await _effective(owner, dive) == sources


async def test_an_implausible_borrowed_calibration_is_no_calibration(owner):
    """Dive 518's case: borrowing a known-wrong fit is the worst case."""
    lab = await _tenant(owner, "lab")
    source = await _dive(owner, lab, "/source")
    dive = await _dive(owner, lab, "/dive", source=source)
    await _accepted(owner, lab, source, laser_position=json.dumps([0.026, 0, 0]))

    assert await _effective(owner, dive) is None


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
