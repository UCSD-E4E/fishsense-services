"""The database side of dive ingest, tenant-scoped (PLAN.md §6.3).

Ports v1's ingest writes (fishsense-lite@a8b2c3bc: create_dive_activity,
finalize_dive_activity, and the API's image upsert) as functions run inside a
tenant transaction as the app role, so RLS applies. v1's semantics are kept:

* a dive is created at **low** whatever was asked -- priority is the commit
  flag, and every cohort ignores low -- and upserts on its path;
* a capture upserts on its path; the **first** capture with a checksum is
  canonical, excluding itself, so a resumed scan never demotes its own dive;
* finalize sets the asked-for priority and the scanned max timestamp;
* containment = shared checksums / this dive's checksums, excluding itself.

v2: all of it is per tenant -- including which copy of a frame is canonical.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import (
    IngestCatalog,
    NotAMember,
    content_overlap,
    create_dive,
    dive_by_path,
    dives_with_leaf,
    finalize_dive,
    register_capture,
    registered_captures,
    resolve_device,
    slate_template_by_name,
)

T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)
A, B, C = ("a" * 32, "b" * 32, "c" * 32)


async def _tenant(owner_engine, slug: str) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _device(owner_engine, tenant, serial="BJ6C67989", name="FSL-07"):
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO devices (tenant_id, kind, serial, name) "
                    "VALUES (:t, 'lite', :s, :n) RETURNING id"
                ),
                {"t": tenant, "s": serial, "n": name},
            )
        ).scalar_one()


async def _dive(app_engine, tenant, path, device=None) -> uuid.UUID:
    async with tenant_transaction(app_engine, tenant) as conn:
        return await create_dive(
            conn, tenant, source_path=path, name=path.rsplit("/", 1)[-1],
            dived_at=T0, device_id=device,
        )  # fmt: skip


async def _register(app_engine, tenant, dive, path, checksum, device=None):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await register_capture(
            conn, tenant, dive_id=dive, device_id=device, source_path=path,
            captured_at=T0, checksum=checksum,
        )  # fmt: skip


# --- device resolution --------------------------------------------------------


async def test_a_serial_resolves_to_the_tenants_device_and_its_calibration(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    device = await _device(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        before = await resolve_device(conn, lab, "BJ6C67989")
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO camera_calibrations (tenant_id, device_id, "
                "camera_matrix, distortion_coefficients) VALUES (:t, :d, "
                "'[[1,0,0],[0,1,0],[0,0,1]]', '[0,0,0,0,0]')"
            ),
            {"t": lab, "d": device},
        )
    async with tenant_transaction(app_engine, lab) as conn:
        after = await resolve_device(conn, lab, "BJ6C67989")

    assert (before.device_id, before.name, before.has_camera_calibration) == (
        device,
        "FSL-07",
        False,
    )
    assert after.has_camera_calibration is True


async def test_an_unknown_serial_or_another_tenants_device_does_not_resolve(
    owner_engine, app_engine
):
    lab, partner = await _tenant(owner_engine, "lab"), await _tenant(
        owner_engine, "partner"
    )
    await _device(owner_engine, partner, serial="PARTNER-CAM")

    async with tenant_transaction(app_engine, lab) as conn:
        assert await resolve_device(conn, lab, "NOPE") is None
        assert await resolve_device(conn, lab, "PARTNER-CAM") is None


# --- create: low, whatever was asked; upsert on path ---------------------------


async def test_a_dive_is_created_at_low_and_rerunning_returns_the_same_dive(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    first = await _dive(app_engine, lab, "2024 REEF/d1")
    async with owner_engine.begin() as conn:  # it was committed high meanwhile
        await conn.execute(text("UPDATE dives SET priority = 'high'"))

    again = await _dive(app_engine, lab, "2024 REEF/d1")

    async with owner_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, name, priority FROM dives"))).all()
    assert again == first
    assert [tuple(r) for r in rows] == [(first, "d1", "low")]


# --- register: upsert on path; first checksum is canonical, per tenant ----------


async def test_the_first_copy_of_a_frame_is_canonical_later_ones_are_not(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")
    d2 = await _dive(app_engine, lab, "d2")

    first = await _register(app_engine, lab, d1, "d1/P1.ORF", A)
    duplicate = await _register(app_engine, lab, d2, "d2/P1.ORF", A)

    assert first.is_canonical is True
    assert duplicate.is_canonical is False


async def test_re_registering_a_path_keeps_its_row_and_its_canonical_copy(
    owner_engine, app_engine
):
    """A resumed scan re-posts paths it already wrote; excluding itself is what
    stops it from demoting its own dive by colliding with itself."""
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")

    first = await _register(app_engine, lab, d1, "d1/P1.ORF", A)
    again = await _register(app_engine, lab, d1, "d1/P1.ORF", A)

    assert again == first
    assert again.is_canonical is True


async def test_canonical_is_decided_per_tenant(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine, "lab"), await _tenant(
        owner_engine, "partner"
    )
    lab_dive = await _dive(app_engine, lab, "d1")
    partner_dive = await _dive(app_engine, partner, "d1")

    await _register(app_engine, lab, lab_dive, "d1/P1.ORF", A)
    theirs = await _register(app_engine, partner, partner_dive, "d1/P1.ORF", A)

    assert theirs.is_canonical is True


async def test_registered_captures_are_what_a_resumed_scan_skips(
    owner_engine, app_engine
):
    """Path -> captured_at: a skipped frame still feeds the dive's max timestamp,
    so a fully-ingested re-run can report it without downloading anything."""
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")
    d2 = await _dive(app_engine, lab, "d2")
    await _register(app_engine, lab, d2, "d2/P1.ORF", C)
    await _register(app_engine, lab, d1, "d1/P1.ORF", A)
    await _register(app_engine, lab, d1, "d1/P2.ORF", B)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await registered_captures(conn, lab, d1) == {
            "d1/P1.ORF": T0,
            "d1/P2.ORF": T0,
        }


# --- finalize and containment ----------------------------------------------------


async def test_finalize_opens_the_commit_flag_with_the_scanned_timestamp(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")
    scanned_max = datetime(2025, 3, 6, 18, 30, tzinfo=UTC)

    async with tenant_transaction(app_engine, lab) as conn:
        await finalize_dive(conn, lab, d1, priority="high", dived_at=scanned_max)

    async with owner_engine.connect() as conn:
        row = (await conn.execute(text("SELECT priority, dived_at FROM dives"))).one()
    assert tuple(row) == ("high", scanned_max)


async def test_containment_is_shared_checksums_over_this_dives_excluding_itself(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    old = await _dive(app_engine, lab, "old")
    new = await _dive(app_engine, lab, "new")
    for path, checksum in [("old/1", A), ("old/2", B)]:
        await _register(app_engine, lab, old, path, checksum)
    for path, checksum in [("new/1", A), ("new/2", B), ("new/3", C)]:
        await _register(app_engine, lab, new, path, checksum)

    async with tenant_transaction(app_engine, lab) as conn:
        overlap = await content_overlap(conn, lab, new)

    assert [(o.dive_id, o.dive_path, o.shared_images) for o in overlap] == [
        (old, "old", 2)
    ]
    assert overlap[0].containment == pytest.approx(2 / 3)


async def test_re_registering_the_canonical_copy_after_a_duplicate_keeps_it(
    owner_engine, app_engine
):
    """v2 deviation, deliberate. v1 marks a frame canonical only if *no other
    row* has its checksum, so re-posting the canonical copy after a duplicate
    landed would demote it -- and leave the frame with no canonical copy at all,
    invisible to every stage. v2: canonical unless another *canonical* copy
    exists."""
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")
    d2 = await _dive(app_engine, lab, "d2")
    await _register(app_engine, lab, d1, "d1/P1.ORF", A)
    await _register(app_engine, lab, d2, "d2/P1.ORF", A)

    again = await _register(app_engine, lab, d1, "d1/P1.ORF", A)

    async with owner_engine.connect() as conn:
        canonical = (
            await conn.execute(text("SELECT count(*) FROM captures WHERE is_canonical"))
        ).scalar_one()
    assert again.is_canonical is True
    assert canonical == 1


# --- lookups preflight makes ---------------------------------------------------


async def test_a_dive_is_found_by_its_path_only_within_the_tenant(
    owner_engine, app_engine
):
    lab, partner = await _tenant(owner_engine, "lab"), await _tenant(
        owner_engine, "partner"
    )
    mine = await _dive(app_engine, lab, "2025/03/06/082929_FishModels_FSL07")
    await _dive(app_engine, partner, "2025/03/06/other")

    async with tenant_transaction(app_engine, lab) as conn:
        assert (
            await dive_by_path(conn, lab, "2025/03/06/082929_FishModels_FSL07") == mine
        )
        assert await dive_by_path(conn, lab, "2025/03/06/other") is None
        assert await dive_by_path(conn, lab, "2025/03/06") is None


async def test_dives_sharing_a_folder_name_are_found_exactly(owner_engine, app_engine):
    """The prod case: dives 64 and 66 are both `082929_FishModels_FSL07`. The
    match is on the whole leaf -- `_` in a dive name is not a wildcard, and a
    longer name ending in the leaf is a different folder."""
    lab, partner = await _tenant(owner_engine, "lab"), await _tenant(
        owner_engine, "partner"
    )
    leaf = "082929_FishModels_FSL07"
    first = await _dive(app_engine, lab, f"2025/03/06/{leaf}")
    second = await _dive(app_engine, lab, f"backup/{leaf}")
    bare = await _dive(app_engine, lab, leaf)
    await _dive(app_engine, lab, f"2025/03/06/x{leaf}")
    await _dive(app_engine, lab, "2025/03/06/082929XFishModelsXFSL07")
    await _dive(app_engine, partner, f"2025/03/06/{leaf}")

    async with tenant_transaction(app_engine, lab) as conn:
        found = await dives_with_leaf(conn, lab, leaf)

    assert sorted(found, key=lambda r: r[1]) == sorted(
        [
            (bare, leaf),
            (first, f"2025/03/06/{leaf}"),
            (second, f"backup/{leaf}"),
        ],
        key=lambda r: r[1],
    )


async def test_a_slate_template_is_found_by_name(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        slate = (
            await conn.execute(
                text(
                    "INSERT INTO slate_templates (name, reference_points) "
                    "VALUES ('PVC 12in', '[]') RETURNING id"
                )
            )
        ).scalar_one()
    lab = await _tenant(owner_engine, "lab")

    async with tenant_transaction(app_engine, lab) as conn:
        assert await slate_template_by_name(conn, "PVC 12in") == slate
        assert await slate_template_by_name(conn, "pvc 12in") is None


# --- the catalog the orchestrator asks, as its service principal --------------

ORCHESTRATOR = "service:fishsense-orchestrator"


async def test_the_orchestrator_resolves_only_tenants_it_is_a_member_of(
    app_engine, seed_memberships
):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.resolve_tenant("lab") == tenants["lab"]
    assert await catalog.resolve_tenant("partner") is None
    assert await catalog.resolve_tenant("nonexistent") is None


async def test_the_catalog_answers_within_the_tenant(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    lab = tenants["lab"]
    device = await _device(owner_engine, lab)
    dive = await _dive(app_engine, lab, "2025/03/06/082929_FishModels_FSL07")
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)

    resolved = await catalog.resolve_device(lab, "BJ6C67989")
    assert (resolved.device_id, resolved.has_camera_calibration) == (device, False)
    assert await catalog.dive_by_path(lab, "2025/03/06/082929_FishModels_FSL07") == dive
    assert await catalog.dives_with_leaf(lab, "082929_FishModels_FSL07") == [
        (dive, "2025/03/06/082929_FishModels_FSL07")
    ]
    assert await catalog.slate_template("missing") is None


async def test_the_catalog_writes_a_dive_through_the_commit_protocol(
    owner_engine, app_engine, seed_memberships
):
    """create at low, register, finalize to high -- the same store functions,
    as the orchestrator."""
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    device = await _device(owner_engine, lab)
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)
    path = "2025/03/06/082929_FishModels_FSL07"

    dive = await catalog.create_dive(
        lab, source_path=path, name="082929_FishModels_FSL07", dived_at=T0,
        device_id=device,
    )  # fmt: skip
    registered = await catalog.register_capture(
        lab, dive_id=dive, device_id=device, source_path=f"{path}/A.ORF",
        captured_at=T0, checksum=A,
    )  # fmt: skip
    assert await catalog.registered_captures(lab, dive) == {f"{path}/A.ORF": T0}
    assert registered.is_canonical
    await catalog.finalize_dive(lab, dive, priority="high", dived_at=T0)
    assert await catalog.content_overlap(lab, dive) == []

    async with tenant_transaction(app_engine, lab) as conn:
        priority = (
            await conn.execute(
                text("SELECT priority FROM dives WHERE id = :d"), {"d": dive}
            )
        ).scalar_one()
    assert priority == "high"


async def test_the_catalog_rechecks_membership_on_every_tenant_call(
    owner_engine, app_engine, seed_memberships
):
    """A tenant id from preflight is not a standing licence: an ingest that
    loses its membership mid-flight stops at its next call, rather than
    finishing on the strength of an earlier check."""
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)

    with pytest.raises(NotAMember):
        await catalog.dive_by_path(tenants["partner"], "anything")

    assert await catalog.dive_by_path(tenants["lab"], "anything") is None
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM memberships WHERE tenant_id = :t"),
                           {"t": tenants["lab"]})  # fmt: skip
    with pytest.raises(NotAMember):
        await catalog.dive_by_path(tenants["lab"], "anything")
