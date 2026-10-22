"""A tenant's numbers for its Today page (docs/design/README.md, Home pages).

Lengths and the wild fish among them, missions and deployments, frames (each
counted once: the same frames filed in several dumps are copies, and only the
canonical one counts), the missions whose port nobody knows, and the latest
activity. Only the tenant's own rows count.
"""

from __future__ import annotations

import pytest
from depth_measure_seed import T0, exec_, forget_identities  # noqa: F401
from observation_seed import ALICE, lite, mobile
from sqlalchemy import event

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.summary_store import tenant_summary


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member", "partner": "member"}})


async def _summary(app_engine, tenant_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await tenant_summary(conn, tenant_id)


async def test_an_empty_tenant_has_nothing_yet(app_engine, tenants):
    s = await _summary(app_engine, tenants["lab"])

    assert (s.lengths, s.wild_fish, s.species, s.missions, s.deployments, s.frames) == (
        0, 0, 0, 0, 0, 0,
    )  # fmt: skip
    assert s.latest_activity is None


async def test_it_counts_lengths_and_the_wild_fish_among_them(
    owner_engine, app_engine, tenants
):
    lab = tenants["lab"]
    await lite(owner_engine, lab, species=("Lachnolaimus maximus", "Hogfish"))
    await lite(owner_engine, lab, species=("Scarus coeruleus", "Blue Parrotfish"),
               device_name="FSL-01")  # fmt: skip
    await lite(owner_engine, lab, model=("George", 0.3127), device_name="FSL-02")
    await mobile(app_engine, owner_engine, lab)

    s = await _summary(app_engine, lab)

    assert s.lengths == 4
    # The fish model is a decoy of known length; the phone's fish is wild.
    assert s.wild_fish == 3
    assert s.species == 2
    assert s.latest_activity is not None


async def test_frames_count_each_capture_once(owner_engine, app_engine, tenants):
    lab = tenants["lab"]
    capture_id, _ = await lite(owner_engine, lab, species=("Scarus coeruleus", "Blue"))
    before = (await _summary(app_engine, lab)).frames
    # A copy of the same frame from another dump: not canonical, not another frame.
    await exec_(
        owner_engine,
        """
        INSERT INTO captures (tenant_id, mission_id, source_path, captured_at,
                              checksum, is_canonical)
        SELECT tenant_id, mission_id, source_path || '-copy', captured_at,
               checksum, false
        FROM captures WHERE id = :c
        """,
        c=capture_id,
    )

    assert (await _summary(app_engine, lab)).frames == before


async def test_it_counts_missions_deployments_and_unknown_ports(
    owner_engine, app_engine, tenants
):
    lab = tenants["lab"]
    await lite(owner_engine, lab, species=("Scarus coeruleus", "Blue"))
    await exec_(
        owner_engine,
        "INSERT INTO deployments (tenant_id, name) VALUES (:t, 'trip')",
        t=lab,
    )
    await exec_(
        owner_engine,
        "INSERT INTO missions (tenant_id, source_path, started_at) "
        "VALUES (:t, 'unknown/port', :at)",
        t=lab,
        at=T0,
    )

    s = await _summary(app_engine, lab)

    assert (s.missions, s.deployments, s.unknown_ports) == (2, 1, 1)


async def test_another_tenants_rows_do_not_count(owner_engine, app_engine, tenants):
    await lite(owner_engine, tenants["partner"], species=("Scarus coeruleus", "Blue"))

    s = await _summary(app_engine, tenants["lab"])

    assert (s.lengths, s.missions, s.frames) == (0, 0, 0)


def _scans(plan, relation: str) -> int:
    """How many plan nodes read ``relation``."""
    here = int(plan.get("Relation Name") == relation)
    return here + sum(_scans(p, relation) for p in plan.get("Plans", []))


async def _plan(conn, statement: str, parameters=()):
    found = await conn.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters)
    return found.scalar_one()[0]["Plan"]


async def test_it_reads_the_observations_once(app_engine, tenants):
    """Lengths, wild fish and species from one pass over `observations`. Each
    pass rebuilds every current measurement in the tenant: three of them took
    the summary to 4.5 s on production (2026-10-21), one takes 0.9 s. Planned
    as `tenant_summary` runs it, against the view's own plan."""
    lab = tenants["lab"]
    ran = []

    def record(conn, cursor, statement, parameters, context, executemany):
        ran.append((statement, parameters))

    async with tenant_transaction(app_engine, lab) as conn:
        event.listen(conn.sync_connection, "before_cursor_execute", record)
        try:
            await tenant_summary(conn, lab)
        finally:
            event.remove(conn.sync_connection, "before_cursor_execute", record)
        view = await _plan(
            conn, "SELECT * FROM observations WHERE tenant_id = $1", (lab,)
        )
        summaries = [await _plan(conn, *query) for query in ran]

    for relation in ("measurements", "species_labels"):
        assert sum(_scans(p, relation) for p in summaries) == _scans(view, relation)
