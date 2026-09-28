"""Integer numbers: what people and tools call a row by.

v1 had integer ids everywhere: Label Studio project titles embed the dive's
(`#{dive_id}`), the web portal links by them, research queries join on them.
v2's ids are UUIDs, and `v1_id` is set only on migrated rows. So every table
that has a `v1_id` also has a `number` (docs/port-plan.md): equal to `v1_id`
for a migrated row, and the next number above v1's for a new one. Titles,
links and the v1-shaped research views use `number`.
"""

import uuid

import pytest
from sqlalchemy import text

from fishsense_services_api.numbers import advance_numbers


async def _tables_with(conn, column: str) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = :c "
            "AND table_name IN (SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE')"
        ),
        {"c": column},
    )
    return set(rows.scalars())


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def _tenant(conn) -> uuid.UUID:
    return (
        await conn.execute(
            text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
            {"s": f"t-{uuid.uuid4().hex[:8]}"},
        )
    ).scalar_one()


async def _dive(conn, tenant, **columns):
    names = ", ".join(["tenant_id", "source_path", "dived_at", *columns])
    values = ", ".join([":t", ":p", "now()", *(f":{c}" for c in columns)])
    return (
        await conn.execute(
            text(f"INSERT INTO dives ({names}) VALUES ({values}) RETURNING number"),
            {"t": tenant, "p": f"d-{uuid.uuid4()}", **columns},
        )
    ).scalar_one()


async def test_every_table_with_a_v1_id_has_a_number(owner):
    assert await _tables_with(owner, "v1_id") <= await _tables_with(owner, "number")


async def test_a_migrated_row_is_numbered_by_its_v1_id(owner):
    tenant = await _tenant(owner)

    assert await _dive(owner, tenant, v1_id=412) == 412


async def test_a_new_row_is_numbered_above_every_v1_id(owner):
    """Once the migration has advanced the numbers, a new dive can't take a
    number a v1 dive -- and its Label Studio projects -- already has."""
    tenant = await _tenant(owner)
    await _dive(owner, tenant, v1_id=90_000)
    await advance_numbers(owner)

    assert await _dive(owner, tenant) > 90_000


async def test_new_rows_are_numbered_distinctly(owner):
    tenant = await _tenant(owner)

    numbers = [await _dive(owner, tenant) for _ in range(3)]

    assert len(set(numbers)) == 3


async def test_a_number_is_unique(owner):
    tenant = await _tenant(owner)
    await _dive(owner, tenant, v1_id=7)

    with pytest.raises(Exception, match="unique|duplicate"):
        async with owner.begin_nested():
            await _dive(owner, tenant, number=7)
