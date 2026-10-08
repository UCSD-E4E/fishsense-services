"""The database side of path repair: a capture whose file moved on the NAS is
pointed at where it went.

New in v2 (2026-10-07). Frames were moved into subfolders on the NAS after
ingest (dives 219, 237, 249), so their rows named files that no longer
existed and every stage that staged them failed. The orchestrator finds each
moved frame and has the NAS hash it; this store lists a dive's frames, says
whether a path is already some capture's, and re-points one -- only the row
the orchestrator checked, only from the path and checksum it checked.

Pinned here:

* a dive's captures with a NAS path, in path order (a frame held only in the
  object store has nothing to repair);
* a re-point is conditional on the row's current path **and** checksum, so it
  can only ever move the row that was verified, and says whether it did;
* a path another capture already holds is visible to the orchestrator, which
  reports it rather than trip `UNIQUE (tenant_id, source_path)`;
* everything is per tenant, and the catalog acts only as a member.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.capture_path_store import (
    CapturePathCatalog,
    PathCapture,
    captures_with_paths,
    path_holder,
    repoint_capture,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.service_principal import NotAMember

T0 = datetime(2023, 10, 19, 9, 30, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"
OLD = "2024.06.20.REEF/102023_Alligator/101923_Alligator/101923_Alligator_FSL02"
NEW = f"{OLD}/101823_Alligator2_FSL02"


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


async def _dive(owner_engine, tenant, path=OLD) -> tuple[uuid.UUID, int]:
    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO dives (tenant_id, source_path, dived_at) "
                    "VALUES (:t, :p, :at) RETURNING id, number"
                ),
                {"t": tenant, "p": path, "at": T0},
            )
        ).one()
    return row.id, row.number


async def _capture(owner_engine, tenant, dive, *, checksum, path=None,
                   canonical=True) -> tuple[uuid.UUID, int]:  # fmt: skip
    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO captures (tenant_id, dive_id, source_path, "
                    "raw_object_key, captured_at, checksum, is_canonical) "
                    "VALUES (:t, :d, :path, :key, :at, :sum, :canon) "
                    "RETURNING id, number"
                ),
                {"t": tenant, "d": dive, "path": path,
                 "key": None if path else f"uploads/{checksum}.ORF", "at": T0,
                 "sum": checksum, "canon": canonical},
            )
        ).one()  # fmt: skip
    return row.id, row.number


async def _path_of(owner_engine, capture_id) -> str | None:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("SELECT source_path FROM captures WHERE id = :c"),
                {"c": capture_id},
            )
        ).scalar_one()


async def _in(app_engine, tenant, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await fn(conn, tenant, *args, **kwargs)


# -- a dive's frames ------------------------------------------------------------


async def test_a_dives_located_captures_in_path_order(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab)
    b, b_n = await _capture(owner_engine, lab, dive, path=f"{OLD}/P2.ORF",
                            checksum=_md5(2), canonical=False)  # fmt: skip
    a, a_n = await _capture(owner_engine, lab, dive, path=f"{OLD}/P1.ORF",
                            checksum=_md5(1))  # fmt: skip
    await _capture(owner_engine, lab, dive, checksum=_md5(3))  # object store only

    assert await _in(app_engine, lab, captures_with_paths, dive) == [
        PathCapture(a, a_n, f"{OLD}/P1.ORF", _md5(1), "md5", True),
        PathCapture(b, b_n, f"{OLD}/P2.ORF", _md5(2), "md5", False),
    ]


async def test_who_holds_a_path(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab)
    held, _ = await _capture(owner_engine, lab, dive, path=f"{NEW}/P1.ORF",
                             checksum=_md5(1))  # fmt: skip

    assert await _in(app_engine, lab, path_holder, f"{NEW}/P1.ORF") == held
    assert await _in(app_engine, lab, path_holder, f"{NEW}/P9.ORF") is None


# -- the re-point -----------------------------------------------------------------


async def test_a_capture_is_pointed_at_where_its_file_went(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab)
    moved, _ = await _capture(owner_engine, lab, dive, path=f"{OLD}/P1.ORF",
                              checksum=_md5(1))  # fmt: skip

    done = await _in(
        app_engine, lab, repoint_capture, moved,
        old_path=f"{OLD}/P1.ORF", new_path=f"{NEW}/P1.ORF", checksum=_md5(1),
    )  # fmt: skip

    assert done is True
    assert await _path_of(owner_engine, moved) == f"{NEW}/P1.ORF"


@pytest.mark.parametrize(
    "old_path, checksum",
    [(f"{OLD}/P9.ORF", _md5(1)), (f"{OLD}/P1.ORF", _md5(9))],
    ids=["another-path", "another-checksum"],
)
async def test_only_the_row_that_was_checked_is_moved(
    owner_engine, app_engine, old_path, checksum
):
    """The orchestrator compared one row's checksum with one file on the NAS.
    A row that has since changed -- another path, another checksum -- is not
    the row it checked, and stays where it is."""
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab)
    moved, _ = await _capture(owner_engine, lab, dive, path=f"{OLD}/P1.ORF",
                              checksum=_md5(1))  # fmt: skip

    done = await _in(
        app_engine, lab, repoint_capture, moved,
        old_path=old_path, new_path=f"{NEW}/P1.ORF", checksum=checksum,
    )  # fmt: skip

    assert done is False
    assert await _path_of(owner_engine, moved) == f"{OLD}/P1.ORF"


async def test_another_tenants_capture_cannot_be_moved(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    reef = await _tenant(owner_engine, "reef")
    dive, _ = await _dive(owner_engine, reef)
    theirs, _ = await _capture(owner_engine, reef, dive, path=f"{OLD}/P1.ORF",
                               checksum=_md5(1))  # fmt: skip

    done = await _in(
        app_engine, lab, repoint_capture, theirs,
        old_path=f"{OLD}/P1.ORF", new_path=f"{NEW}/P1.ORF", checksum=_md5(1),
    )  # fmt: skip

    assert done is False
    assert await _path_of(owner_engine, theirs) == f"{OLD}/P1.ORF"


# -- the catalog, as the orchestrator's principal --------------------------------


async def test_the_catalog_repairs_within_a_tenant_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive, number = await _dive(owner_engine, lab)
    moved, _ = await _capture(owner_engine, lab, dive, path=f"{OLD}/P1.ORF",
                              checksum=_md5(1))  # fmt: skip
    catalog = CapturePathCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert await catalog.dive_by_number(lab, number) == dive
    assert [c.id for c in await catalog.captures_with_paths(lab, dive)] == [moved]
    assert await catalog.path_holder(lab, f"{OLD}/P1.ORF") == moved
    assert await catalog.repoint_capture(
        lab, moved, old_path=f"{OLD}/P1.ORF", new_path=f"{NEW}/P1.ORF",
        checksum=_md5(1),
    )  # fmt: skip
    assert await _path_of(owner_engine, moved) == f"{NEW}/P1.ORF"


async def test_the_catalog_refuses_a_tenant_it_is_not_a_member_of(
    app_engine, seed_memberships
):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = CapturePathCatalog(app_engine, sub=ORCHESTRATOR)

    with pytest.raises(NotAMember):
        await catalog.repoint_capture(
            tenants["partner"], uuid.uuid4(), old_path="a", new_path="b",
            checksum=_md5(1),
        )  # fmt: skip
