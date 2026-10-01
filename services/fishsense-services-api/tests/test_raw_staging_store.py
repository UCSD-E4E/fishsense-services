"""The database side of raw staging and its cleanup, tenant-scoped.

v1 had no endpoint of its own for this: staging and cleanup both read
`fs.images.get(dive_id=...)` (every image row of the dive) and filtered
client-side (fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/stage_raw_bytes_for_dive_activity.py and
cleanup_raw_bytes_for_dive_activity.py). v1's rules, kept:

* **staging is canonical-only**, mirroring every cohort, or the per-image work
  dispatched would not match what the cohort promised and the dive could never
  drain;
* a frame with no NAS path is still returned, for staging to count as
  ``no_path`` rather than silently drop.

v2 changes, each pinned here:

* everything is per tenant, and the catalog acts only as a member;
* **cleanup deletes only scratch this dive owns.** v1 deleted the checksum of
  every image row of the dive, canonical or not, to evict scratch staged before
  the canonical gate existed. But a duplicate shares its key with its canonical
  twin *in another dive*, so v1's cleanup could delete a frame another dive's
  child was mid-read on -- and the scratch-in-use gate, which looks only at
  this dive's children, could not see it. v2's scratch is new (every key under
  ``tenants/``), so there is no pre-gate scratch to evict: cleanup takes the
  dive's checksums except those whose canonical copy is in another dive;
* **a capture says whether it came from v1**, so the processed-JPEG check can
  find the JPEG v1 wrote -- and never for a frame v2 ingested.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import create_dive, register_capture
from fishsense_services_api.raw_staging_store import (
    CaptureChecksum,
    RawStagingCatalog,
    StagingCapture,
    capture_checksum,
    checksums_to_clean,
    captures_to_stage,
)
from fishsense_services_api.service_principal import NotAMember

T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"


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


async def _dive(app_engine, tenant, path) -> uuid.UUID:
    async with tenant_transaction(app_engine, tenant) as conn:
        return await create_dive(conn, tenant, source_path=path, name=path, dived_at=T0)


async def _capture(app_engine, tenant, dive, name, checksum):
    """A frame registered as ingest registers it: canonical unless another
    canonical copy of the content exists in the tenant."""
    async with tenant_transaction(app_engine, tenant) as conn:
        registered = await register_capture(
            conn, tenant, dive_id=dive, device_id=None,
            source_path=f"{dive}/{name}", captured_at=T0, checksum=checksum,
        )  # fmt: skip
    return registered.capture_id


async def _raw_capture(owner_engine, tenant, dive, checksum, *, v1_id=None,
                       source_path=None, canonical=True):  # fmt: skip
    """A capture written directly: one migrated from v1, or one held only in
    the object store (no NAS path)."""
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO captures (tenant_id, v1_id, dive_id, source_path, "
                    "raw_object_key, captured_at, checksum, is_canonical) "
                    "VALUES (:t, :v1, :d, :path, :key, :at, :sum, :canon) "
                    "RETURNING id"
                ),
                {"t": tenant, "v1": v1_id, "d": dive, "path": source_path,
                 "key": None if source_path else f"uploads/{checksum}.ORF",
                 "at": T0, "sum": checksum, "canon": canonical},
            )
        ).scalar_one()  # fmt: skip


async def _stage(app_engine, tenant, dive):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await captures_to_stage(conn, tenant, dive)


async def _clean(app_engine, tenant, dive):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await checksums_to_clean(conn, tenant, dive)


# -- staging ---------------------------------------------------------------------


async def test_staging_takes_the_dives_canonical_captures_with_path_and_checksum(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "2024.06.20.REEF/dive_42")
    a = await _capture(app_engine, lab, dive, "P1.ORF", _md5(1))
    b = await _capture(app_engine, lab, dive, "P2.ORF", _md5(2))

    assert await _stage(app_engine, lab, dive) == [
        StagingCapture(a, f"{dive}/P1.ORF", _md5(1)),
        StagingCapture(b, f"{dive}/P2.ORF", _md5(2)),
    ]


async def test_staging_skips_a_duplicate_whose_canonical_copy_is_another_dives(
    owner_engine, app_engine
):
    """Canonical only (v1): the other dive stages that frame."""
    lab = await _tenant(owner_engine)
    first = await _dive(app_engine, lab, "first")
    second = await _dive(app_engine, lab, "second")
    await _capture(app_engine, lab, first, "P1.ORF", _md5(1))
    await _capture(app_engine, lab, second, "P1-copy.ORF", _md5(1))
    own = await _capture(app_engine, lab, second, "P2.ORF", _md5(2))

    assert [c.capture_id for c in await _stage(app_engine, lab, second)] == [own]


async def test_a_frame_with_no_nas_path_is_returned_for_staging_to_count(
    owner_engine, app_engine
):
    """Never silently dropped: staging counts it as `no_path` (v1)."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d")
    capture = await _raw_capture(owner_engine, lab, dive, _md5(3))

    assert await _stage(app_engine, lab, dive) == [
        StagingCapture(capture, None, _md5(3))
    ]


async def test_staging_sees_only_the_tenants_own_dive(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reef = await _tenant(owner_engine, "reef")
    dive = await _dive(app_engine, lab, "d")
    await _capture(app_engine, lab, dive, "P1.ORF", _md5(1))

    assert await _stage(app_engine, reef, dive) == []
    assert await _clean(app_engine, reef, dive) == []


# -- cleanup ---------------------------------------------------------------------


async def test_cleanup_takes_every_checksum_this_dive_owns(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d")
    await _capture(app_engine, lab, dive, "P1.ORF", _md5(1))
    await _capture(app_engine, lab, dive, "P2.ORF", _md5(2))

    assert await _clean(app_engine, lab, dive) == [_md5(1), _md5(2)]


async def test_cleanup_leaves_scratch_another_dive_owns(owner_engine, app_engine):
    """v2 change. The duplicate's key is its canonical twin's, which another
    dive staged and whose child may be reading it right now; that dive's own
    cleanup, gated on its own children, deletes it."""
    lab = await _tenant(owner_engine)
    first = await _dive(app_engine, lab, "first")
    second = await _dive(app_engine, lab, "second")
    await _capture(app_engine, lab, first, "P1.ORF", _md5(1))
    await _capture(app_engine, lab, second, "P1-copy.ORF", _md5(1))
    await _capture(app_engine, lab, second, "P2.ORF", _md5(2))

    assert await _clean(app_engine, lab, second) == [_md5(2)]
    assert await _clean(app_engine, lab, first) == [_md5(1)]


async def test_cleanup_still_evicts_a_frame_no_dive_holds_canonical(
    owner_engine, app_engine
):
    """Broader than staging where it is safe (v1's reason for not filtering):
    a frame demoted after it was staged, with no canonical copy anywhere, is
    nobody else's scratch."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d")
    await _raw_capture(
        owner_engine, lab, dive, _md5(4), source_path="d/P4.ORF", canonical=False
    )

    assert await _clean(app_engine, lab, dive) == [_md5(4)]


async def test_cleanup_names_each_object_once(owner_engine, app_engine):
    """Two rows of one frame in the dive are one scratch object."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d")
    await _capture(app_engine, lab, dive, "P1.ORF", _md5(1))
    await _capture(app_engine, lab, dive, "P1-again.ORF", _md5(1))

    assert await _clean(app_engine, lab, dive) == [_md5(1)]


# -- the processed-JPEG check ----------------------------------------------------


async def test_a_capture_migrated_from_v1_says_so(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d")
    migrated = await _raw_capture(
        owner_engine, lab, dive, _md5(5), v1_id=4242, source_path="d/P5.ORF"
    )
    ingested = await _capture(app_engine, lab, dive, "P6.ORF", _md5(6))

    async with tenant_transaction(app_engine, lab) as conn:
        assert await capture_checksum(conn, lab, migrated) == CaptureChecksum(
            _md5(5), from_v1=True
        )
        assert await capture_checksum(conn, lab, ingested) == CaptureChecksum(
            _md5(6), from_v1=False
        )
        assert await capture_checksum(conn, lab, uuid.uuid4()) is None


async def test_another_tenants_capture_is_not_found(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reef = await _tenant(owner_engine, "reef")
    dive = await _dive(app_engine, lab, "d")
    capture = await _capture(app_engine, lab, dive, "P1.ORF", _md5(1))

    async with tenant_transaction(app_engine, reef) as conn:
        assert await capture_checksum(conn, reef, capture) is None


# -- the catalog, as the orchestrator's principal --------------------------------


async def test_the_catalog_answers_within_a_tenant_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive = await _dive(app_engine, lab, "d")
    capture = await _capture(app_engine, lab, dive, "P1.ORF", _md5(1))
    catalog = RawStagingCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.captures_to_stage(lab, dive) == [
        StagingCapture(capture, f"{dive}/P1.ORF", _md5(1))
    ]
    assert await catalog.checksums_to_clean(lab, dive) == [_md5(1)]
    assert await catalog.capture_checksum(lab, capture) == CaptureChecksum(
        _md5(1), from_v1=False
    )


async def test_the_catalog_refuses_a_tenant_it_is_not_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = RawStagingCatalog(app_engine, sub=ORCHESTRATOR)

    with pytest.raises(NotAMember):
        await catalog.captures_to_stage(tenants["partner"], uuid.uuid4())
