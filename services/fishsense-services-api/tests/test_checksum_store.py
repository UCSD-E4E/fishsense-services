"""The database side of checksum verification, tenant-scoped and read-only.

v1 had no store of its own for this: `verify_dive_checksums_activity` read
`fs.images.get(dive_id=...)` (every image row of the dive), and the sweep's
selector read `GET /api/v1/canonical/dives/` (dive_controller.py:46), the dives
with at least one canonical image (fishsense-lite@77e8f8e5). v1's rules, kept:

* **verification is NOT canonical-filtered.** The duplicates are half of v1's
  image rows and exactly the population whose provenance is least certain, so
  excluding them would skip the rows most worth checking;
* the sweep visits every dive with **at least one canonical** capture;
* rows come back **in path order** (v1 sorted by `path or ""`), so a sample of
  the first N is the same N every time.

v2 changes, each pinned here:

* everything is per tenant, and the catalog acts only as a member;
* a dive and a capture are named by their `number` -- v1's id for a migrated
  row -- because that is what an operator types and what v1's reports said.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.checksum_store import (
    ChecksumCatalog,
    VerifyCapture,
    canonical_dive_numbers,
    captures_to_verify,
    dive_by_number,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.service_principal import NotAMember

T0 = datetime(2024, 8, 21, 8, 56, 51, tzinfo=UTC)
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


async def _dive(owner_engine, tenant, path, *, v1_id=None) -> tuple[uuid.UUID, int]:
    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO dives (tenant_id, v1_id, source_path, dived_at) "
                    "VALUES (:t, :v1, :p, :at) RETURNING id, number"
                ),
                {"t": tenant, "v1": v1_id, "p": path, "at": T0},
            )
        ).one()
    return row.id, row.number


async def _capture(owner_engine, tenant, dive, *, checksum, path=None, v1_id=None,
                   canonical=True, algorithm="md5", at=T0):  # fmt: skip
    """A capture as migrate-v1 writes one: any path, canonical or not, and
    (with no path) held only in the object store."""
    async with owner_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO captures (tenant_id, v1_id, dive_id, source_path, "
                    "raw_object_key, captured_at, checksum, checksum_algorithm, "
                    "is_canonical) "
                    "VALUES (:t, :v1, :d, :path, :key, :at, :sum, :alg, :canon) "
                    "RETURNING id, number"
                ),
                {"t": tenant, "v1": v1_id, "d": dive, "path": path,
                 "key": None if path else f"uploads/{checksum}.ORF", "at": at,
                 "sum": checksum, "alg": algorithm, "canon": canonical},
            )
        ).one()  # fmt: skip
    return row.number


async def _verify(app_engine, tenant, dive):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await captures_to_verify(conn, tenant, dive)


# -- the frames of one dive ------------------------------------------------------


async def test_every_capture_of_the_dive_is_verified_in_path_order(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab, "2024.06.20.REEF/082929")
    b = await _capture(owner_engine, lab, dive, path="d/P2.ORF", checksum=_md5(2))
    a = await _capture(owner_engine, lab, dive, path="d/P1.ORF", checksum=_md5(1))

    assert await _verify(app_engine, lab, dive) == [
        VerifyCapture(a, "d/P1.ORF", _md5(1), "md5", T0),
        VerifyCapture(b, "d/P2.ORF", _md5(2), "md5", T0),
    ]


async def test_duplicates_are_verified_too(owner_engine, app_engine):
    """NOT canonical-filtered (v1): the duplicates are the rows whose
    provenance is least certain."""
    lab = await _tenant(owner_engine)
    first, _ = await _dive(owner_engine, lab, "first")
    second, _ = await _dive(owner_engine, lab, "second")
    await _capture(owner_engine, lab, first, path="first/P1.ORF", checksum=_md5(1))
    dup = await _capture(owner_engine, lab, second, path="second/P1.ORF",
                         checksum=_md5(1), canonical=False)  # fmt: skip

    assert [c.number for c in await _verify(app_engine, lab, second)] == [dup]


async def test_a_migrated_capture_is_named_by_its_v1_id(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab, "d", v1_id=412)
    await _capture(owner_engine, lab, dive, path="d/P1.ORF", checksum=_md5(1),
                   v1_id=81234)  # fmt: skip

    assert [c.number for c in await _verify(app_engine, lab, dive)] == [81234]


async def test_a_capture_with_no_nas_path_sorts_first_as_in_v1(
    owner_engine, app_engine
):
    """v1 sorted on `path or ""`; the activity skips a pathless row, but where
    it sorts decides which rows a sample of the first N takes."""
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab, "d")
    await _capture(owner_engine, lab, dive, path="d/P1.ORF", checksum=_md5(1))
    held = await _capture(owner_engine, lab, dive, checksum=_md5(2))

    first = (await _verify(app_engine, lab, dive))[0]
    assert (first.number, first.source_path) == (held, None)


async def test_the_recorded_algorithm_comes_back(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab, "d")
    await _capture(owner_engine, lab, dive, path="d/P1.ORF", checksum="ab" * 32,
                   algorithm="sha256")  # fmt: skip

    (capture,) = await _verify(app_engine, lab, dive)
    assert capture.checksum_algorithm == "sha256"


async def test_another_tenants_dive_has_no_captures(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reef = await _tenant(owner_engine, "reef")
    dive, _ = await _dive(owner_engine, lab, "d")
    await _capture(owner_engine, lab, dive, path="d/P1.ORF", checksum=_md5(1))

    assert await _verify(app_engine, reef, dive) == []


# -- finding a dive by its number ------------------------------------------------


async def test_a_dive_is_found_by_its_number(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _dive(owner_engine, lab, "d", v1_id=412)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await dive_by_number(conn, lab, 412) == dive
        assert await dive_by_number(conn, lab, 413) is None


async def test_another_tenants_dive_is_not_found_by_number(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reef = await _tenant(owner_engine, "reef")
    await _dive(owner_engine, lab, "d", v1_id=412)

    async with tenant_transaction(app_engine, reef) as conn:
        assert await dive_by_number(conn, reef, 412) is None


# -- the sweep's dives (v1's GET /api/v1/canonical/dives/) -----------------------


async def test_the_sweep_takes_dives_with_a_canonical_capture_in_number_order(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    later, _ = await _dive(owner_engine, lab, "later", v1_id=66)
    earlier, _ = await _dive(owner_engine, lab, "earlier", v1_id=64)
    duplicates_only, _ = await _dive(owner_engine, lab, "dups", v1_id=65)
    await _dive(owner_engine, lab, "empty", v1_id=67)
    await _capture(owner_engine, lab, later, path="l/P1.ORF", checksum=_md5(1))
    await _capture(owner_engine, lab, earlier, path="e/P2.ORF", checksum=_md5(2))
    await _capture(owner_engine, lab, duplicates_only, path="x/P1.ORF",
                   checksum=_md5(1), canonical=False)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await canonical_dive_numbers(conn, lab) == [64, 66]


async def test_the_sweep_sees_only_the_tenants_dives(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reef = await _tenant(owner_engine, "reef")
    dive, _ = await _dive(owner_engine, lab, "d", v1_id=64)
    await _capture(owner_engine, lab, dive, path="d/P1.ORF", checksum=_md5(1))

    async with tenant_transaction(app_engine, reef) as conn:
        assert await canonical_dive_numbers(conn, reef) == []


# -- the catalog, as the orchestrator's principal --------------------------------


async def test_the_catalog_answers_within_a_tenant_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive, number = await _dive(owner_engine, lab, "d")
    capture = await _capture(owner_engine, lab, dive, path="d/P1.ORF",
                             checksum=_md5(1))  # fmt: skip
    catalog = ChecksumCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert await catalog.dive_by_number(lab, number) == dive
    assert await catalog.canonical_dive_numbers(lab) == [number]
    assert [c.number for c in await catalog.captures_to_verify(lab, dive)] == [capture]


async def test_the_catalog_refuses_a_tenant_it_is_not_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = ChecksumCatalog(app_engine, sub=ORCHESTRATOR)

    with pytest.raises(NotAMember):
        await catalog.captures_to_verify(tenants["partner"], uuid.uuid4())


def test_the_store_only_reads():
    """Read-only by construction, like v1's activity: it runs against
    production data to answer a question and must not be able to change the
    answer."""
    import inspect

    from fishsense_services_api import checksum_store

    source = inspect.getsource(checksum_store).upper()
    for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP"):
        assert verb not in source, f"the checksum store must only read: {verb}"
