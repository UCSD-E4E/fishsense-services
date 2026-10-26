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
from fishsense_services_api.mission_store import PortIsRuled
from fishsense_services_api.ingest_store import (
    IngestCatalog,
    NotAMember,
    content_overlap,
    create_mission,
    mission_by_path,
    missions_with_leaf,
    finalize_mission,
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
        return await create_mission(
            conn, tenant, source_path=path, name=path.rsplit("/", 1)[-1],
            started_at=T0, device_id=device,
        )  # fmt: skip


async def _register(app_engine, tenant, mission, path, checksum, device=None):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await register_capture(
            conn, tenant, mission_id=mission, device_id=device, source_path=path,
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


async def test_a_mission_is_created_at_low_and_rerunning_returns_the_same_mission(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    first = await _dive(app_engine, lab, "2024 REEF/d1")
    async with owner_engine.begin() as conn:  # it was committed high meanwhile
        await conn.execute(text("UPDATE missions SET priority = 'high'"))

    again = await _dive(app_engine, lab, "2024 REEF/d1")

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, name, priority FROM missions"))
        ).all()
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
    lab_mission = await _dive(app_engine, lab, "d1")
    partner_mission = await _dive(app_engine, partner, "d1")

    await _register(app_engine, lab, lab_mission, "d1/P1.ORF", A)
    theirs = await _register(app_engine, partner, partner_mission, "d1/P1.ORF", A)

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


async def test_a_capture_keeps_its_focal_length_and_a_rescan_without_one_keeps_it(
    owner_engine, app_engine
):
    """The zoom gate's input (0069): ingest records EXIF 0x920A. A re-scan
    that read none (an older caller) must not erase what was recorded."""
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")
    async with tenant_transaction(app_engine, lab) as conn:
        first = await register_capture(
            conn, lab, mission_id=d1, device_id=None, source_path="d1/P1.ORF",
            captured_at=T0, checksum=A, focal_length_mm=14.62,
        )  # fmt: skip
    await _register(app_engine, lab, d1, "d1/P1.ORF", A)

    async with owner_engine.begin() as conn:
        focal = (
            await conn.execute(
                text("SELECT focal_length_mm FROM captures WHERE id = :c"),
                {"c": first.capture_id},
            )
        ).scalar_one()
    assert focal == pytest.approx(14.62)


# --- finalize and containment ----------------------------------------------------


async def test_finalize_opens_the_commit_flag_with_the_scanned_timestamp(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    d1 = await _dive(app_engine, lab, "d1")
    scanned_max = datetime(2025, 3, 6, 18, 30, tzinfo=UTC)

    async with tenant_transaction(app_engine, lab) as conn:
        await finalize_mission(conn, lab, d1, priority="high", started_at=scanned_max)

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT priority, started_at FROM missions"))
        ).one()
    assert tuple(row) == ("high", scanned_max)


async def test_containment_is_shared_checksums_over_this_missions_excluding_itself(
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

    assert [(o.mission_id, o.mission_path, o.shared_images) for o in overlap] == [
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


async def test_a_mission_is_found_by_its_path_only_within_the_tenant(
    owner_engine, app_engine
):
    lab, partner = await _tenant(owner_engine, "lab"), await _tenant(
        owner_engine, "partner"
    )
    mine = await _dive(app_engine, lab, "2025/03/06/082929_FishModels_FSL07")
    await _dive(app_engine, partner, "2025/03/06/other")

    async with tenant_transaction(app_engine, lab) as conn:
        assert (
            await mission_by_path(conn, lab, "2025/03/06/082929_FishModels_FSL07")
            == mine
        )
        assert await mission_by_path(conn, lab, "2025/03/06/other") is None
        assert await mission_by_path(conn, lab, "2025/03/06") is None


async def test_missions_sharing_a_folder_name_are_found_exactly(
    owner_engine, app_engine
):
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
        found = await missions_with_leaf(conn, lab, leaf)

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
    mission = await _dive(app_engine, lab, "2025/03/06/082929_FishModels_FSL07")
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)

    resolved = await catalog.resolve_device(lab, "BJ6C67989")
    assert (resolved.device_id, resolved.has_camera_calibration) == (device, False)
    assert (
        await catalog.mission_by_path(lab, "2025/03/06/082929_FishModels_FSL07")
        == mission
    )
    assert await catalog.missions_with_leaf(lab, "082929_FishModels_FSL07") == [
        (mission, "2025/03/06/082929_FishModels_FSL07")
    ]
    assert await catalog.slate_template("missing") is None


async def test_the_catalog_writes_a_mission_through_the_commit_protocol(
    owner_engine, app_engine, seed_memberships
):
    """create at low, register, finalize to high -- the same store functions,
    as the orchestrator."""
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    device = await _device(owner_engine, lab)
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)
    path = "2025/03/06/082929_FishModels_FSL07"

    mission = await catalog.create_mission(
        lab, source_path=path, name="082929_FishModels_FSL07", started_at=T0,
        device_id=device,
    )  # fmt: skip
    registered = await catalog.register_capture(
        lab, mission_id=mission, device_id=device, source_path=f"{path}/A.ORF",
        captured_at=T0, checksum=A,
    )  # fmt: skip
    assert await catalog.registered_captures(lab, mission) == {f"{path}/A.ORF": T0}
    assert registered.is_canonical
    await catalog.finalize_mission(lab, mission, priority="high", started_at=T0)
    assert await catalog.content_overlap(lab, mission) == []

    async with tenant_transaction(app_engine, lab) as conn:
        priority = (
            await conn.execute(
                text("SELECT priority FROM missions WHERE id = :d"), {"d": mission}
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
        await catalog.mission_by_path(tenants["partner"], "anything")

    assert await catalog.mission_by_path(tenants["lab"], "anything") is None
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM memberships WHERE tenant_id = :t"),
                           {"t": tenants["lab"]})  # fmt: skip
    with pytest.raises(NotAMember):
        await catalog.mission_by_path(tenants["lab"], "anything")


# --- the port and purpose (0047) ----------------------------------------------


async def _create(app_engine, tenant, path, **facts):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await create_mission(
            conn, tenant, source_path=path, name="m", started_at=T0, **facts
        )


async def _port_and_purpose(owner_engine, mission):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT port, port_source, port_note, purpose FROM missions "
                    "WHERE id = :m"
                ),
                {"m": mission},
            )
        ).one()


async def test_a_mission_is_created_with_its_port_and_purpose(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")

    mission = await _create(
        app_engine, lab, "/d/1", port="flat", port_source="operator",
        port_note="pool test", purpose="other",
    )  # fmt: skip

    assert tuple(await _port_and_purpose(owner_engine, mission)) == (
        "flat",
        "operator",
        "pool test",
        "other",
    )


async def test_re_ingesting_without_a_statement_keeps_what_is_known(
    owner_engine, app_engine
):
    """A re-ingest of a backfilled mission says nothing about its port; that
    must not reset the owner's rule to unknown, nor clear a stated purpose."""
    lab = await _tenant(owner_engine, "lab")
    mission = await _create(
        app_engine, lab, "/d/1", port="corrective_optic", port_source="backfill",
        port_note="the owner's rule", purpose="survey",
    )  # fmt: skip

    again = await _create(app_engine, lab, "/d/1")

    assert again == mission
    assert tuple(await _port_and_purpose(owner_engine, mission)) == (
        "corrective_optic",
        "backfill",
        "the owner's rule",
        "survey",
    )


async def test_re_ingesting_cannot_contradict_the_owners_rule(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    mission = await _create(
        app_engine, lab, "/d/1", port="corrective_optic", port_source="device_suffix"
    )

    with pytest.raises(PortIsRuled):
        await _create(app_engine, lab, "/d/1", port="flat", port_source="operator")

    assert (await _port_and_purpose(owner_engine, mission)).port == "corrective_optic"


async def test_re_ingesting_with_a_statement_replaces_an_operators(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    mission = await _create(
        app_engine, lab, "/d/1", port="flat", port_source="operator"
    )

    await _create(app_engine, lab, "/d/1", port="dome", port_source="operator")

    assert (await _port_and_purpose(owner_engine, mission)).port == "dome"


async def test_the_catalog_reads_a_missions_port_by_path(app_engine, seed_memberships):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    lab = tenants["lab"]
    await _create(
        app_engine, lab, "/d/1", port="corrective_optic", port_source="backfill"
    )
    catalog = IngestCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.mission_port(lab, "/d/1") == ("corrective_optic", "backfill")
    assert await catalog.mission_port(lab, "/d/2") is None
