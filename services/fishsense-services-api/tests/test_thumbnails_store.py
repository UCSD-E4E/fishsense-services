"""Each render's thumbnail (0053): which renders are owed one, and what was
made. A thumbnail is made from the stored render -- never the NAS -- for the
renders in the tenant's current bucket; a render that was not there to read is
recorded too, so it is not tried every hour."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    capture,
    device,
    forget_identities,
    mission,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.renders_store import RenderRow, persist_renders
from fishsense_services_api.thumbnails_store import (
    InvalidThumbnails,
    ThumbnailRow,
    ThumbnailsCatalog,
    owed_thumbnails,
    persist_thumbnails,
)
from observation_seed import render

W = 640
BUCKET = "fishsense-tenant-lab"
MD5 = "0123456789abcdef0123456789abcdef"


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _rendered(owner_engine, lab, **kw):
    m = await mission(owner_engine, lab, device_id=await device(owner_engine, lab))
    c = await capture(owner_engine, lab, m)
    await _exec(owner_engine, "UPDATE captures SET device_id = (SELECT device_id "
                "FROM missions WHERE id = :m) WHERE id = :c", m=m, c=c)  # fmt: skip
    key = await render(owner_engine, lab, c, **kw)
    render_id = (await _rows(owner_engine, "SELECT id FROM capture_renders "
                             "WHERE key = :k", k=key))[0]["id"]  # fmt: skip
    return m, c, render_id, key


async def _exec(engine, sql, **params):
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _rows(engine, sql, **params):
    async with engine.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql), params)]


def _made(render_id, capture_id, **extra):
    fields = dict(render_id=render_id, capture_id=capture_id, status="made",
                  target_width=W, bucket=BUCKET, key=f"thumbnails/w{W}/r.JPG",
                  bytes=40_000, md5=MD5, width=640, height=481)  # fmt: skip
    fields.update(extra)
    return ThumbnailRow(**fields)


async def test_a_render_in_the_current_bucket_is_owed_its_thumbnail(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c, render_id, key = await _rendered(owner_engine, lab)
    await _rendered(owner_engine, lab, bucket="old-bucket")
    _, _, abstained, _ = await _rendered(owner_engine, lab)
    await _exec(owner_engine, "UPDATE capture_renders SET status = 'decode_failed', "
                "bucket = NULL, key = NULL, bytes = NULL, md5 = NULL, width = NULL, "
                "height = NULL WHERE id = :r", r=abstained)  # fmt: skip

    owed = await _in(app_engine, lab, owed_thumbnails, bucket=BUCKET, width=W)

    assert [(o.render_id, o.capture_id, o.render_key) for o in owed] == [
        (render_id, c, key)
    ]


async def test_a_thumbnail_made_or_a_render_missing_settles_it(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c1, r1, _ = await _rendered(owner_engine, lab)
    _, c2, r2, _ = await _rendered(owner_engine, lab)
    await _in(app_engine, lab, persist_thumbnails, [_made(r1, c1)])
    await _in(app_engine, lab, persist_thumbnails, [ThumbnailRow(
        render_id=r2, capture_id=c2, status="render_unavailable", target_width=W)])  # fmt: skip

    assert await _in(app_engine, lab, owed_thumbnails, bucket=BUCKET, width=W) == []
    # Another width owes them again.
    owed = await _in(app_engine, lab, owed_thumbnails, bucket=BUCKET, width=320)
    assert {o.render_id for o in owed} == {r1, r2}


async def test_owed_thumbnails_come_a_page_at_a_time(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    renders = [(await _rendered(owner_engine, lab))[2] for _ in range(3)]

    first = await _in(app_engine, lab, owed_thumbnails, bucket=BUCKET, width=W, limit=2)

    assert len(first) == 2 and {o.render_id for o in first} <= set(renders)


async def test_a_thumbnail_is_recorded_once_whole_and_for_its_own_capture(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c, r, _ = await _rendered(owner_engine, lab)
    _, _, other_render, _ = await _rendered(owner_engine, lab)

    written = [await _in(app_engine, lab, persist_thumbnails, [_made(r, c)])
               for _ in range(2)]  # fmt: skip
    with pytest.raises(InvalidThumbnails):
        await _in(app_engine, lab, persist_thumbnails, [_made(other_render, c)])
    with pytest.raises(InvalidThumbnails):
        await _in(app_engine, lab, persist_thumbnails, [_made(uuid.uuid4(), c)])
    with pytest.raises(IntegrityError):
        await _in(app_engine, lab, persist_thumbnails,
                  [_made(r, c, target_width=320, md5="x")])  # fmt: skip

    assert written == [1, 0]


async def test_the_catalog_serves_only_member_tenants(app_engine, seed_memberships):
    tenants = await seed_memberships(
        {"service:fishsense-orchestrator": {"lab": "member"}}
    )
    catalog = ThumbnailsCatalog(app_engine, sub="service:fishsense-orchestrator")

    assert await catalog.member_tenants() == [tenants["lab"]]
