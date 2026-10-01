"""Dives and captures: the core of the Lite path (PLAN.md §4.3, §6.4).

Row-level isolation is covered for every table by the schema audit. These
tests pin what the audit can't see: the database itself keeps references
inside one tenant (composite foreign keys), and the constraints carry v1's
semantics -- priority as the commit/park flag, one canonical copy of a frame
per tenant, md5 checksums, and nothing deleted out from under history.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fishsense_services_api.db import tenant_transaction

MD5 = "0123456789abcdef0123456789abcdef"


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant(conn, slug: str) -> uuid.UUID:
    return await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )


async def _dive(conn, tenant_id, path: str, **columns) -> uuid.UUID:
    names = ", ".join(["tenant_id", "source_path", "dived_at", *columns])
    values = ", ".join([":tenant_id", ":path", "now()", *(f":{c}" for c in columns)])
    return await _one(
        conn,
        f"INSERT INTO dives ({names}) VALUES ({values}) RETURNING id",
        tenant_id=tenant_id,
        path=path,
        **columns,
    )


async def _capture(conn, tenant_id, dive_id, path: str, **columns) -> uuid.UUID:
    columns = {"checksum": MD5, **columns}
    names = ", ".join(["tenant_id", "dive_id", "source_path", "captured_at", *columns])
    values = ", ".join(
        [":tenant_id", ":dive_id", ":path", "now()", *(f":{c}" for c in columns)]
    )
    return await _one(
        conn,
        f"INSERT INTO captures ({names}) VALUES ({values}) RETURNING id",
        tenant_id=tenant_id,
        dive_id=dive_id,
        path=path,
        **columns,
    )


@pytest.fixture
async def owner(owner_engine):
    """An owner connection, committed per test (cleaned up by conftest)."""
    async with owner_engine.begin() as conn:
        yield conn


# --- the database keeps references inside one tenant ---------------------------


async def test_a_capture_cannot_belong_to_another_tenants_dive(owner_engine):
    async with owner_engine.begin() as conn:
        lab, partner = await _tenant(conn, "lab"), await _tenant(conn, "partner")
        partners_dive = await _dive(conn, partner, "/dives/p1")

    with pytest.raises(IntegrityError, match="foreign key"):
        async with owner_engine.begin() as conn:
            await _capture(conn, lab, partners_dive, "/dives/p1/P0001.ORF")


async def test_a_dive_cannot_borrow_another_tenants_calibration(owner_engine):
    async with owner_engine.begin() as conn:
        lab, partner = await _tenant(conn, "lab"), await _tenant(conn, "partner")
        partners_dive = await _dive(conn, partner, "/dives/p1")

    with pytest.raises(IntegrityError, match="foreign key"):
        async with owner_engine.begin() as conn:
            await _dive(
                conn, lab, "/dives/l1", calibration_source_dive_id=partners_dive
            )


async def test_a_dive_cannot_borrow_its_own_calibration(owner_engine):
    async with owner_engine.begin() as conn:
        lab = await _tenant(conn, "lab")
        dive = await _dive(conn, lab, "/dives/l1")

    with pytest.raises(IntegrityError, match="check"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE dives SET calibration_source_dive_id = id WHERE id = :d"),
                {"d": dive},
            )


# --- v1 semantics, carried as constraints ---------------------------------------


async def test_priority_defaults_to_low_and_rejects_unknown_values(owner):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/dives/l1")

    assert (
        await _one(owner, "SELECT priority FROM dives WHERE id = :d", d=dive) == "low"
    )
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _dive(owner, lab, "/dives/l2", priority="urgent")


async def test_a_frame_is_canonical_once_per_tenant(owner):
    lab, partner = await _tenant(owner, "lab"), await _tenant(owner, "partner")
    lab_dive, lab_dup = await _dive(owner, lab, "/a"), await _dive(owner, lab, "/b")
    partner_dive = await _dive(owner, partner, "/c")

    await _capture(owner, lab, lab_dive, "/a/1.ORF", is_canonical=True)
    await _capture(owner, partner, partner_dive, "/c/1.ORF", is_canonical=True)
    await _capture(owner, lab, lab_dup, "/b/1.ORF", is_canonical=False)
    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await _capture(owner, lab, lab_dup, "/b/2.ORF", is_canonical=True)


async def test_an_md5_checksum_must_look_like_one(owner):
    lab = await _tenant(owner, "lab")
    dive = await _dive(owner, lab, "/a")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _capture(owner, lab, dive, "/a/1.ORF", checksum="NOT-AN-MD5")


async def test_a_capture_needs_a_source_path_or_an_object_key(owner):
    lab = await _tenant(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _capture(owner, lab, None, None)


# --- nothing is deleted out from under history -----------------------------------


async def test_a_dive_with_captures_cannot_be_deleted(owner_engine):
    async with owner_engine.begin() as conn:
        lab = await _tenant(conn, "lab")
        dive = await _dive(conn, lab, "/a")
        await _capture(conn, lab, dive, "/a/1.ORF")

    with pytest.raises(IntegrityError, match="foreign key"):
        async with owner_engine.begin() as conn:
            await conn.execute(text("DELETE FROM dives WHERE id = :d"), {"d": dive})


async def test_deleting_a_tenant_removes_all_of_its_data(owner_engine):
    async with owner_engine.begin() as conn:
        lab = await _tenant(conn, "lab")
        dive = await _dive(conn, lab, "/a")
        await _capture(conn, lab, dive, "/a/1.ORF")
        await _dive(conn, lab, "/b", calibration_source_dive_id=dive)

    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": lab})
        remaining = await _one(
            conn,
            "SELECT (SELECT count(*) FROM dives) + (SELECT count(*) FROM captures)",
        )

    assert remaining == 0


# --- the app role, inside its tenant ---------------------------------------------


async def test_the_app_role_records_a_dive_and_capture_in_its_tenant(
    owner_engine, app_engine
):
    async with owner_engine.begin() as conn:
        lab = await _tenant(conn, "lab")

    async with tenant_transaction(app_engine, lab) as conn:
        dive = await _dive(conn, lab, "/a", priority="high")
        await _capture(conn, lab, dive, "/a/1.ORF", is_canonical=True)
        count = await _one(conn, "SELECT count(*) FROM captures")

    assert count == 1


async def test_devices_carry_a_known_kind(owner):
    lab = await _tenant(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await owner.execute(
                text(
                    "INSERT INTO devices (tenant_id, kind, serial) "
                    "VALUES (:t, 'toaster', 'X')"
                ),
                {"t": lab},
            )
