"""Phone captures imported from a dataset on the NAS (0058; the owner,
2026-10-21: CCFRP's summer 2025 FishSense Mobile data).

An imported capture was never synced by anyone, so it has no uploader. It
says where it came from instead (``imported_from``), and a capture with a
client id has exactly one of the two. Otherwise it is a phone capture like any
other: its files, its manifest kept as given, and a phone mission by the
owner's rule (0057). Importing again changes nothing.
"""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_phone_missions import T0, _capture, _mission_of

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import commit_capture, mobile_capture
from fishsense_services_api.mobile_import_store import (
    MobileImportCatalog,
    NotAnImport,
)

ORCHESTRATOR = "service:orchestrator"
SOURCE = "/fishsense_data/CCFRP/2025.06.27.FishSense.SanDiego/ED-04/iPhone/FSM"


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ORCHESTRATOR: {"lab": "member"}})


def _imported(**changes):
    capture = replace(
        _capture(None, place="SIO"),
        imported_from=f"{SOURCE}/database.sqlite#photos/12",
        manifest={"import": {"intrinsics_source": "paper@e769bdd"}},
    )
    return replace(capture, **changes)


@pytest.fixture
def catalog(app_engine):
    return MobileImportCatalog(app_engine, sub=ORCHESTRATOR)


async def test_an_imported_capture_says_where_it_came_from_and_has_no_uploader(
    tenants, catalog, app_engine, owner_engine
):
    capture = _imported()

    committed = await catalog.import_capture(tenants["lab"], capture)

    assert committed.created
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        found = await mobile_capture(conn, tenants["lab"], capture.client_capture_uuid)
    assert found.uploaded_by is None
    assert found.imported_from == f"{SOURCE}/database.sqlite#photos/12"
    assert found.manifest == {"import": {"intrinsics_source": "paper@e769bdd"}}
    mission = await _mission_of(owner_engine, committed.capture_id)
    assert (mission.source, mission.name) == ("mobile", "2026.10.11.FishSense.SIO")


async def test_importing_again_changes_nothing(tenants, catalog, owner_engine):
    capture = _imported()
    await catalog.import_capture(tenants["lab"], capture)

    again = await catalog.import_capture(tenants["lab"], capture)

    assert not again.created
    async with owner_engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM captures"))
        ).scalar_one() == 1


async def test_the_catalog_imports_only_what_says_where_it_came_from(tenants, catalog):
    with pytest.raises(NotAnImport):
        await catalog.import_capture(tenants["lab"], _imported(imported_from=None))


async def test_an_import_has_no_uploader(tenants, catalog, owner_engine):
    async with owner_engine.connect() as conn:
        someone = (
            await conn.execute(text("SELECT id FROM users LIMIT 1"))
        ).scalar_one()

    with pytest.raises(NotAnImport):
        await catalog.import_capture(tenants["lab"], _imported(uploaded_by=someone))


async def test_the_database_wants_exactly_one_of_uploader_and_source(
    tenants, app_engine, owner_engine
):
    async with owner_engine.connect() as conn:
        someone = (
            await conn.execute(text("SELECT id FROM users LIMIT 1"))
        ).scalar_one()
    for uploaded_by, imported_from in ((None, None), (someone, "a/source")):
        capture = _imported(uploaded_by=uploaded_by, imported_from=imported_from)
        with pytest.raises(IntegrityError):
            async with tenant_transaction(app_engine, tenants["lab"]) as conn:
                await commit_capture(conn, tenants["lab"], capture, [])


async def test_the_catalog_lists_what_is_already_imported(tenants, catalog):
    first, other = _imported(), _imported()
    await catalog.import_capture(tenants["lab"], first)

    found = await catalog.imported(
        tenants["lab"], [first.client_capture_uuid, other.client_capture_uuid]
    )

    assert found == {first.client_capture_uuid}


async def test_a_synced_capture_is_unchanged(tenants, app_engine, owner_engine):
    """The app's own sync still binds a capture to whoever synced it."""
    async with owner_engine.connect() as conn:
        someone = (
            await conn.execute(text("SELECT id FROM users LIMIT 1"))
        ).scalar_one()
    capture = _capture(someone)
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        await commit_capture(conn, tenants["lab"], capture, [])
        found = await mobile_capture(conn, tenants["lab"], capture.client_capture_uuid)

    assert (found.uploaded_by, found.imported_from) == (someone, None)
    assert found.captured_at == T0
    assert uuid.UUID(str(found.client_capture_uuid))
