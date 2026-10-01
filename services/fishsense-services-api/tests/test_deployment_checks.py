"""What the post-converge smoke test asks the database, owned here.

The smoke test (orchestrator ops/smoke.py, PLAN.md §6.6 step 6) runs in the
orchestrator's image, but only this package touches the database (repo-root
tests/test_database_ownership.py). So its questions are asked here, and the
smoke test only opens the connections they run on.
"""

from __future__ import annotations

import uuid

from fishsense_services_api.deployment_checks import (
    lab_tenant_id,
    migration_revision,
    research_measurement_count,
)
from fishsense_services_api.migrations import head_revision

from depth_measure_seed import tenant
from research_seed import Lab


async def test_the_migration_revision_is_the_head(owner_engine):
    async with owner_engine.connect() as conn:
        assert await migration_revision(conn) == head_revision()


async def test_the_lab_tenant_is_found_by_its_slug(owner_engine):
    lab = await tenant(owner_engine, "lab")
    await tenant(owner_engine, "reef")

    async with owner_engine.connect() as conn:
        assert await lab_tenant_id(conn) == lab


async def test_no_lab_tenant_is_none(owner_engine):
    await tenant(owner_engine, "reef")

    async with owner_engine.connect() as conn:
        assert await lab_tenant_id(conn) is None


async def test_a_dives_measurements_are_counted_through_the_research_views(
    owner_engine, research_engine
):
    """Through v1's shape, as imwut's and cscw's extracts join it: the
    research login reads the lab's measurements by v1's dive id."""
    lab = Lab(owner_engine, await tenant(owner_engine, "lab"))
    capture_id, _ = await lab.measure(dive=1, model="Snook", length_m=0.5)
    await lab.measure(dive=1, model="Snook", length_m=0.6)
    await lab.measure(dive=2, model="Snook", length_m=0.7)
    dive_number = await lab.number("dives", (await lab.dive(1))[0])

    async with research_engine.connect() as conn:
        assert await research_measurement_count(conn, dive_number) == 2
        assert await research_measurement_count(conn, 10**9) == 0
