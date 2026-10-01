"""Fish and frame clusters (v1's ``fish``, ``diveframecluster`` + mapping).

A fish is either a real animal of some species, or a physical fish model /
calibration target (Grouper, Ruler …) -- never both. A model is a
``fish_models`` row, joined by key, not matched by name (§2.7); each tenant has
one fish per model. Frames are grouped into clusters, from prediction (stage 1)
or Label Studio regrouping (stage 6.1); a cluster is bound to its fish at
measurement. Fish are never deleted: measurements point at them.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant(conn, slug: str) -> uuid.UUID:
    return await _one(
        conn, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )


async def _dive_with_capture(conn, tenant) -> tuple[uuid.UUID, uuid.UUID]:
    dive = await _one(
        conn,
        "INSERT INTO dives (tenant_id, source_path, dived_at) "
        "VALUES (:t, :p, now()) RETURNING id",
        t=tenant,
        p=f"/{uuid.uuid4().hex}",
    )
    capture = await _one(
        conn,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, checksum) "
        "VALUES (:t, :d, :p, now(), '0123456789abcdef0123456789abcdef') RETURNING id",
        t=tenant,
        d=dive,
        p=f"/{uuid.uuid4().hex}.ORF",
    )
    return dive, capture


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn
    async with owner_engine.begin() as conn:
        await conn.execute(text("TRUNCATE fish_models, species CASCADE"))


async def test_a_fish_is_a_species_or_a_model_never_both(owner):
    lab = await _tenant(owner, "lab")
    species = await _one(
        owner,
        "INSERT INTO species (scientific_name) VALUES ('Epinephelus') RETURNING id",
    )
    model = await _one(
        owner, "INSERT INTO fish_models (name) VALUES ('Grouper') RETURNING id"
    )
    insert = (
        "INSERT INTO fish (tenant_id, species_id, fish_model_id) VALUES (:t, :s, :m)"
    )

    await owner.execute(text(insert), {"t": lab, "s": species, "m": None})
    await owner.execute(text(insert), {"t": lab, "s": None, "m": model})
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await owner.execute(text(insert), {"t": lab, "s": species, "m": model})


async def test_a_tenant_has_one_fish_per_model(owner):
    lab, partner = await _tenant(owner, "lab"), await _tenant(owner, "partner")
    model = await _one(
        owner, "INSERT INTO fish_models (name) VALUES ('Ruler') RETURNING id"
    )
    insert = "INSERT INTO fish (tenant_id, fish_model_id) VALUES (:t, :m)"

    await owner.execute(text(insert), {"t": lab, "m": model})
    await owner.execute(text(insert), {"t": partner, "m": model})
    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await owner.execute(text(insert), {"t": lab, "m": model})


async def test_fish_are_never_deleted(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        lab = await _tenant(conn, "lab")

    async with tenant_transaction(app_engine, lab) as conn:
        await conn.execute(text("INSERT INTO fish (tenant_id) VALUES (:t)"), {"t": lab})

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_transaction(app_engine, lab) as conn:
            await conn.execute(text("DELETE FROM fish"))


async def test_a_cluster_knows_how_it_was_formed(owner):
    lab = await _tenant(owner, "lab")
    dive, _ = await _dive_with_capture(owner, lab)
    insert = (
        "INSERT INTO dive_frame_clusters (tenant_id, dive_id, formed_by) "
        "VALUES (:t, :d, :f)"
    )

    await owner.execute(text(insert), {"t": lab, "d": dive, "f": "prediction"})
    await owner.execute(text(insert), {"t": lab, "d": dive, "f": "label_studio"})
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await owner.execute(text(insert), {"t": lab, "d": dive, "f": "vibes"})


async def test_a_capture_sits_in_a_cluster_once_and_leaves_with_it(owner):
    lab = await _tenant(owner, "lab")
    dive, capture = await _dive_with_capture(owner, lab)
    cluster = await _one(
        owner,
        "INSERT INTO dive_frame_clusters (tenant_id, dive_id, formed_by) "
        "VALUES (:t, :d, 'prediction') RETURNING id",
        t=lab,
        d=dive,
    )
    member = (
        "INSERT INTO dive_frame_cluster_captures (tenant_id, cluster_id, capture_id) "
        "VALUES (:t, :c, :p)"
    )
    await owner.execute(text(member), {"t": lab, "c": cluster, "p": capture})

    with pytest.raises(IntegrityError, match="duplicate key"):
        async with owner.begin_nested():
            await owner.execute(text(member), {"t": lab, "c": cluster, "p": capture})

    await owner.execute(
        text("DELETE FROM dive_frame_clusters WHERE id = :c"), {"c": cluster}
    )
    assert await _one(owner, "SELECT count(*) FROM dive_frame_cluster_captures") == 0


async def test_only_a_migrated_cluster_may_lack_its_dive_or_formation(owner):
    lab = await _tenant(owner, "lab")

    await owner.execute(
        text("INSERT INTO dive_frame_clusters (tenant_id, v1_id) VALUES (:t, 9)"),
        {"t": lab},
    )
    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await owner.execute(
                text("INSERT INTO dive_frame_clusters (tenant_id) VALUES (:t)"),
                {"t": lab},
            )
