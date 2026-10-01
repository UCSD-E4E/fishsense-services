"""Label Studio labels: laser, head/tail, dive slate, species (PLAN.md §4.3).

Labels mirror Label Studio, so sync updates them in place -- but they are never
deleted: ``superseded`` records retirement. Each carries its **source** (human,
gate auto-accept, model pre-annotation, import), which v1 cannot recover
(§2.1); only migrated rows may leave it unknown. A **sentinel** (a label with no
Label Studio project) carries no task either.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction

LABEL_TABLES = ["laser_labels", "head_tail_labels", "slate_labels", "species_labels"]


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant_and_capture(conn, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )
    dive = await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "VALUES (:t, :p, now()) RETURNING id",
        t=tenant,
        p=f"/{slug}",
    )
    capture = await _one(
        conn,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, checksum) "
        "VALUES (:t, :d, :p, now(), '0123456789abcdef0123456789abcdef') RETURNING id",
        t=tenant,
        d=dive,
        p=f"/{slug}/1.ORF",
    )
    return tenant, capture


async def _label(conn, table, tenant, capture, **columns) -> uuid.UUID:
    values = {"source": "human", **columns}
    names = ", ".join(["tenant_id", "capture_id", *values])
    params = ", ".join([":tenant_id", ":capture_id", *(f":{c}" for c in values)])
    return await _one(
        conn,
        f"INSERT INTO {table} ({names}) VALUES ({params}) RETURNING id",
        tenant_id=tenant,
        capture_id=capture,
        **values,
    )


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


@pytest.mark.parametrize("table", LABEL_TABLES)
async def test_sync_updates_a_label_but_nothing_deletes_one(
    owner_engine, app_engine, table
):
    async with owner_engine.begin() as conn:
        tenant, capture = await _tenant_and_capture(conn, "lab")

    async with tenant_transaction(app_engine, tenant) as conn:
        await _label(conn, table, tenant, capture, ls_project_id=7, ls_task_id=70)
        await conn.execute(text(f"UPDATE {table} SET completed = true"))

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_transaction(app_engine, tenant) as conn:
            await conn.execute(text(f"DELETE FROM {table}"))


@pytest.mark.parametrize("table", LABEL_TABLES)
async def test_only_a_migrated_label_may_have_an_unknown_source(owner, table):
    tenant, capture = await _tenant_and_capture(owner, "lab")

    await _label(owner, table, tenant, capture, source=None, v1_id=11)
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _label(owner, table, tenant, capture, source=None)


@pytest.mark.parametrize("table", LABEL_TABLES)
async def test_a_sentinel_label_carries_no_task(owner, table):
    tenant, capture = await _tenant_and_capture(owner, "lab")

    await _label(owner, table, tenant, capture)  # a sentinel: no project, no task
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _label(owner, table, tenant, capture, ls_task_id=5)


@pytest.mark.parametrize("table", LABEL_TABLES)
async def test_one_label_studio_task_backs_one_label(owner, table):
    tenant, capture = await _tenant_and_capture(owner, "lab")
    await _label(owner, table, tenant, capture, ls_project_id=7, ls_task_id=70)

    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await _label(owner, table, tenant, capture, ls_project_id=8, ls_task_id=70)


@pytest.mark.parametrize("table", LABEL_TABLES)
async def test_one_label_per_capture_per_project(owner, table):
    tenant, capture = await _tenant_and_capture(owner, "lab")
    await _label(owner, table, tenant, capture, ls_project_id=7, ls_task_id=70)

    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await _label(owner, table, tenant, capture, ls_project_id=7, ls_task_id=71)


async def test_an_unknown_label_source_is_rejected(owner):
    tenant, capture = await _tenant_and_capture(owner, "lab")

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _label(owner, "laser_labels", tenant, capture, source="guesswork")


async def test_sync_cursors_are_one_per_kind_and_project(owner):
    tenant, _ = await _tenant_and_capture(owner, "lab")
    insert = (
        "INSERT INTO label_studio_sync_cursors "
        "(tenant_id, kind, ls_project_id, last_synced_at) VALUES (:t, :k, 7, now())"
    )
    await owner.execute(text(insert), {"t": tenant, "k": "laser"})

    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await owner.execute(text(insert), {"t": tenant, "k": "laser"})
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await owner.execute(text(insert), {"t": tenant, "k": "whale"})
