"""The database side of creating a Label Studio project, tenant-scoped.

v1 had no record of which project held which dive's labels: every create
searched Label Studio for the title `{dive.name} #{dive_id} - {suffix}`
(fishsense-lite@77e8f8e5 populate_utils.build_per_dive_title and
create_or_get_label_studio_project), and the dive's name and id came from the
API. v2 records every project it creates or finds (label_studio_projects,
migration 0020) and looks there first.

What these pin:

* a title embeds the dive's `number`, which is v1's dive id for a migrated
  dive, so v2 builds the titles v1 did and finds v1's projects by them;
* only a record that carries a title is a dive's own project. migrate-v1
  records every project v1's labels point at against the dive holding most of
  its labels, with no title -- which includes the grandfathered shared
  projects (71/76). Trusting those would send one dive's tasks into a project
  shared with other dives, so they are healed by v1's title search instead;
* recording a project found by title heals the migrated record rather than
  adding a second one;
* the newest record wins, so a project recreated after its predecessor was
  deleted in Label Studio is the one found next time.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.label_project_store import (
    KINDS,
    DiveForTitle,
    LabelProjectCatalog,
    RecordedProject,
    dive_for_title,
    record_project,
    recorded_project,
)
from fishsense_services_api.service_principal import NotAMember

ORCHESTRATOR = "service:fishsense-orchestrator"


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _dive(owner_engine, tenant, *, name="Reef 3", v1_id=None) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO dives (tenant_id, source_path, name, dived_at, v1_id) "
                    "VALUES (:t, :p, :n, :at, :v1) RETURNING id"
                ),
                {"t": tenant, "p": f"d-{uuid.uuid4()}", "n": name,
                 "at": datetime(2026, 4, 10, tzinfo=UTC), "v1": v1_id},
            )  # fmt: skip
        ).scalar_one()


async def _migrated(owner_engine, tenant, dive, kind, ls_project_id):
    """A record as migrate-v1 writes it: no title."""
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO label_studio_projects "
                "(tenant_id, dive_id, kind, ls_project_id) VALUES (:t, :d, :k, :p)"
            ),
            {"t": tenant, "d": dive, "k": kind, "p": ls_project_id},
        )


async def _records(owner_engine, tenant):
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT dive_id, kind, ls_project_id, title FROM label_studio_projects "
                "WHERE tenant_id = :t ORDER BY ls_project_id"
            ),
            {"t": tenant},
        )
        return [tuple(r) for r in rows]


def test_the_kinds_are_the_tables():
    """The same kinds as migration 0020's check."""
    assert KINDS == ("laser", "head_tail", "slate", "species", "checkerboard_lattice")


# -- the dive's title parts -----------------------------------------------------------


async def test_a_migrated_dive_is_titled_by_its_v1_id(owner_engine, app_engine):
    """v1's title embeds its dive id; a migrated dive's number is that id, so
    v2 builds the very title v1's project already has."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab, name="101624_AlligatorDeep_FSL02", v1_id=439)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await dive_for_title(conn, lab, dive) == DiveForTitle(
            number=439, name="101624_AlligatorDeep_FSL02"
        )


async def test_an_unknown_dive_has_no_title(owner_engine, app_engine):
    lab = await _tenant(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await dive_for_title(conn, lab, uuid.uuid4()) is None


async def test_another_tenants_dive_has_no_title(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    theirs = await _dive(owner_engine, partner)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await dive_for_title(conn, lab, theirs) is None


# -- recorded projects --------------------------------------------------------------


async def test_a_recorded_project_is_found_by_dive_and_kind(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(conn, lab, "laser", dive_id=dive, ls_project_id=43,
                             title="Reef 3 #1 - Laser Calibration Labeling")  # fmt: skip
        found = await recorded_project(conn, lab, "laser", dive_id=dive,
                                       title="anything")  # fmt: skip
        other_kind = await recorded_project(conn, lab, "species", dive_id=dive,
                                            title="anything")  # fmt: skip

    assert found == RecordedProject(43, "Reef 3 #1 - Laser Calibration Labeling")
    assert other_kind is None


async def test_a_renamed_dive_keeps_its_project(owner_engine, app_engine):
    """v2 change: v1 found a project only by title, so renaming a dive made
    the next populate create a second project and split the dive's labels
    across two. The record is keyed on the dive, not the title."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(conn, lab, "laser", dive_id=dive, ls_project_id=43,
                             title="Old name #1 - Laser Calibration Labeling")  # fmt: skip
        found = await recorded_project(
            conn, lab, "laser", dive_id=dive,
            title="New name #1 - Laser Calibration Labeling",
        )  # fmt: skip

    assert found is not None and found.ls_project_id == 43


async def test_an_untitled_migrated_record_is_not_trusted(owner_engine, app_engine):
    """migrate-v1 records a shared project against the dive holding most of
    its labels, untitled. Finding it here would push this dive's tasks into a
    project other dives share; the title search heals it instead."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    await _migrated(owner_engine, lab, dive, "head_tail", 76)

    async with tenant_transaction(app_engine, lab) as conn:
        assert (
            await recorded_project(conn, lab, "head_tail", dive_id=dive, title="t")
            is None
        )


async def test_recording_a_found_project_heals_the_migrated_record(
    owner_engine, app_engine
):
    """One record per project: the title search's find fills in the migrated
    row (and moves it to the dive the title names), rather than adding one."""
    lab = await _tenant(owner_engine)
    dominant, own = await _dive(owner_engine, lab), await _dive(owner_engine, lab)
    await _migrated(owner_engine, lab, dominant, "laser", 43)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(conn, lab, "laser", dive_id=own, ls_project_id=43,
                             title="Reef #9 - Laser Calibration Labeling")  # fmt: skip

    assert await _records(owner_engine, lab) == [
        (own, "laser", 43, "Reef #9 - Laser Calibration Labeling")
    ]


async def test_the_newest_record_wins(owner_engine, app_engine):
    """A project deleted in Label Studio is recreated and recorded; the next
    lookup must find the new one, not keep probing the deleted one."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(conn, lab, "laser", dive_id=dive, ls_project_id=43,
                             title="t")  # fmt: skip
    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(conn, lab, "laser", dive_id=dive, ls_project_id=12,
                             title="t")  # fmt: skip
        found = await recorded_project(conn, lab, "laser", dive_id=dive, title="t")

    assert found is not None and found.ls_project_id == 12


async def test_a_project_of_no_dive_is_found_by_title(owner_engine, app_engine):
    """The checkerboard lattice study is one project for every dive (v1 keeps
    it blind), so it has no dive to key on; its fixed title is the key."""
    lab = await _tenant(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(
            conn, lab, "checkerboard_lattice", dive_id=None, ls_project_id=500,
            title="Checkerboard Lattice Verification",
        )  # fmt: skip
        found = await recorded_project(
            conn, lab, "checkerboard_lattice", dive_id=None,
            title="Checkerboard Lattice Verification",
        )  # fmt: skip
        other = await recorded_project(
            conn, lab, "checkerboard_lattice", dive_id=None, title="Another study"
        )

    assert found == RecordedProject(500, "Checkerboard Lattice Verification")
    assert other is None


async def test_records_are_the_tenants_own(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    dive = await _dive(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_project(conn, lab, "laser", dive_id=dive, ls_project_id=43,
                             title="t")  # fmt: skip
    async with tenant_transaction(app_engine, partner) as conn:
        assert (
            await recorded_project(conn, partner, "laser", dive_id=dive, title="t")
            is None
        )


# -- the catalog ----------------------------------------------------------------------


async def test_the_catalog_acts_as_a_member(owner_engine, app_engine, seed_memberships):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive = await _dive(owner_engine, lab, v1_id=393)
    catalog = LabelProjectCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.dive_for_title(lab, dive) == DiveForTitle(393, "Reef 3")
    await catalog.record_project(lab, "laser", dive_id=dive, ls_project_id=43,
                                 title="Reef 3 #393 - Laser Calibration Labeling")  # fmt: skip
    assert await catalog.recorded_project(
        lab, "laser", dive_id=dive, title="ignored"
    ) == RecordedProject(43, "Reef 3 #393 - Laser Calibration Labeling")


async def test_the_catalog_refuses_an_unserved_tenant(
    owner_engine, app_engine, seed_memberships
):
    ids = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "u": {"x": "member"}}
    )
    catalog = LabelProjectCatalog(app_engine, sub=ORCHESTRATOR)

    with pytest.raises(NotAMember):
        await catalog.record_project(ids["x"], "laser", dive_id=None,
                                     ls_project_id=1, title="t")  # fmt: skip
