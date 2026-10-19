"""Deployments, and what a mission knows about itself (PLAN.md §4.9, 0047).

Row-level isolation is covered for every table by the schema audit. These pin
what it can't see: a mission joins a deployment only of its own tenant, and the
port is a fact with provenance -- ``unknown`` unless something says otherwise,
and a known port always says what said so.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant(conn, slug: str) -> uuid.UUID:
    return await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )


async def _mission(conn, tenant_id, path: str, **columns) -> uuid.UUID:
    names = ", ".join(["tenant_id", "source_path", "started_at", *columns])
    values = ", ".join([":tenant_id", ":path", "now()", *(f":{c}" for c in columns)])
    return await _one(
        conn,
        f"INSERT INTO missions ({names}) VALUES ({values}) RETURNING id",
        tenant_id=tenant_id,
        path=path,
        **columns,
    )


async def _deployment(conn, tenant_id, name: str) -> uuid.UUID:
    return await _one(
        conn,
        "INSERT INTO deployments (tenant_id, name) VALUES (:t, :n) RETURNING id",
        t=tenant_id,
        n=name,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def test_a_mission_cannot_join_another_tenants_deployment(owner_engine):
    async with owner_engine.begin() as conn:
        lab, partner = await _tenant(conn, "lab"), await _tenant(conn, "partner")
        trip = await _deployment(conn, partner, "2023.08.03.FishSense.Alligator")

    with pytest.raises(IntegrityError, match="foreign key"):
        async with owner_engine.begin() as conn:
            await _mission(conn, lab, "/m/1", deployment_id=trip)


async def test_a_deployment_is_numbered_and_named_once_per_tenant(owner):
    lab, partner = await _tenant(owner, "lab"), await _tenant(owner, "partner")
    first = await _deployment(owner, lab, "2023.08.03.FishSense.Alligator")
    await _deployment(owner, partner, "2023.08.03.FishSense.Alligator")

    numbers = (
        await owner.execute(text("SELECT number FROM deployments ORDER BY number"))
    ).scalars()
    assert len(set(numbers)) == 2
    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await _deployment(owner, lab, "2023.08.03.FishSense.Alligator")
    assert first is not None


async def test_the_port_is_unknown_until_something_says_otherwise(owner):
    lab = await _tenant(owner, "lab")
    mission = await _mission(owner, lab, "/m/1")

    row = (
        await owner.execute(
            text("SELECT port, port_source FROM missions WHERE id = :m"), {"m": mission}
        )
    ).one()
    assert (row.port, row.port_source) == ("unknown", None)


@pytest.mark.parametrize("port", ["corrective_optic", "flat", "dome"])
@pytest.mark.parametrize(
    "source", ["backfill", "device_suffix", "operator", "calibration"]
)
async def test_a_known_port_records_what_said_so(owner, port, source):
    lab = await _tenant(owner, "lab")
    await _mission(owner, lab, "/m/1", port=port, port_source=source)


async def test_a_known_port_without_a_source_is_refused(owner):
    lab = await _tenant(owner, "lab")
    with pytest.raises(IntegrityError, match="missions_port_source_check"):
        async with owner.begin_nested():
            await _mission(owner, lab, "/m/1", port="corrective_optic")


async def test_an_unknown_port_cannot_claim_a_source(owner):
    lab = await _tenant(owner, "lab")
    with pytest.raises(IntegrityError, match="missions_port_source_check"):
        async with owner.begin_nested():
            await _mission(owner, lab, "/m/1", port="unknown", port_source="operator")


async def test_unrecognised_ports_and_sources_are_refused(owner):
    lab = await _tenant(owner, "lab")
    with pytest.raises(IntegrityError, match="missions_port_check"):
        async with owner.begin_nested():
            await _mission(owner, lab, "/m/1", port="pinhole", port_source="operator")
    with pytest.raises(IntegrityError, match="missions_port_source_check"):
        async with owner.begin_nested():
            await _mission(owner, lab, "/m/2", port="flat", port_source="folder_name")


@pytest.mark.parametrize(
    "purpose",
    [
        "survey",
        "lens_calibration",
        "laser_calibration",
        "slate",
        "known_length",
        "other",
    ],
)
async def test_a_mission_states_its_purpose(owner, purpose):
    lab = await _tenant(owner, "lab")
    await _mission(owner, lab, "/m/1", purpose=purpose)


async def test_purpose_is_unstated_by_default_and_checked(owner):
    lab = await _tenant(owner, "lab")
    mission = await _mission(owner, lab, "/m/1")
    assert (
        await _one(owner, "SELECT purpose FROM missions WHERE id = :m", m=mission)
        is None
    )
    with pytest.raises(IntegrityError, match="missions_purpose_check"):
        async with owner.begin_nested():
            await _mission(owner, lab, "/m/2", purpose="fishing")


async def test_a_mission_keeps_its_place_and_raw_metadata(owner):
    lab = await _tenant(owner, "lab")
    mission = await _mission(
        owner,
        lab,
        "/m/1",
        country="USA",
        region="SanDiego",
        site="Alligator",
        source_metadata='{"device": "FSL-10D", "properties": {}}',
    )
    row = (
        await owner.execute(
            text(
                "SELECT country, region, site, source_metadata FROM missions "
                "WHERE id = :m"
            ),
            {"m": mission},
        )
    ).one()
    assert row.country == "USA" and row.site == "Alligator"
    assert row.source_metadata["device"] == "FSL-10D"
