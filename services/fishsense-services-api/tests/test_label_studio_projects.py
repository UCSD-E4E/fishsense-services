"""Which Label Studio project holds which dive's labels of which kind.

v1 found a dive's project only by title (`{name} #{dive_id} - Species
Labeling`), searching Label Studio for it. v2 records the link when it creates
a project, and migrate-v1 fills it from the projects v1's labels point at, so
nothing has to search by title except to heal. Titles keep embedding the
dive's `number` (migration 0019), so they still match v1's.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fishsense_services_api.db import tenant_transaction


@pytest.fixture
async def owner(owner_engine):
    async with owner_engine.begin() as conn:
        yield conn


async def _tenant_and_dive(conn, slug="lab"):
    tenant = (
        await conn.execute(
            text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
            {"s": slug},
        )
    ).scalar_one()
    dive = (
        await conn.execute(
            text(
                "INSERT INTO dives (tenant_id, source_path, dived_at) "
                "VALUES (:t, :p, now()) RETURNING id"
            ),
            {"t": tenant, "p": f"d-{uuid.uuid4()}"},
        )
    ).scalar_one()
    return tenant, dive


async def _project(conn, tenant, dive, kind="laser", project=43, title=None):
    return (
        await conn.execute(
            text(
                "INSERT INTO label_studio_projects "
                "(tenant_id, dive_id, kind, ls_project_id, title) "
                "VALUES (:t, :d, :k, :p, :title) RETURNING id"
            ),
            {"t": tenant, "d": dive, "k": kind, "p": project, "title": title},
        )
    ).scalar_one()


async def test_a_project_is_recorded_per_tenant_kind_and_id(owner):
    tenant, dive = await _tenant_and_dive(owner)
    await _project(owner, tenant, dive, title="Reef 3 #412 - Laser Labeling")

    with pytest.raises(IntegrityError):
        async with owner.begin_nested():
            await _project(owner, tenant, dive)


@pytest.mark.parametrize(
    "kind", ["laser", "head_tail", "slate", "species", "checkerboard_lattice"]
)
async def test_the_kinds_are_v1s_projects(owner, kind):
    tenant, dive = await _tenant_and_dive(owner)

    await _project(owner, tenant, dive, kind=kind)


async def test_an_unknown_kind_is_rejected(owner):
    tenant, dive = await _tenant_and_dive(owner)

    with pytest.raises(IntegrityError, match="check"):
        async with owner.begin_nested():
            await _project(owner, tenant, dive, kind="guesswork")


async def test_projects_are_visible_only_within_the_tenant(owner_engine, app_engine):
    async with owner_engine.begin() as conn:
        lab, lab_dive = await _tenant_and_dive(conn, "lab")
        partner, partner_dive = await _tenant_and_dive(conn, "partner")
        await _project(conn, lab, lab_dive, project=1)
        await _project(conn, partner, partner_dive, project=2)

    async with tenant_transaction(app_engine, lab) as conn:
        seen = (
            await conn.execute(text("SELECT ls_project_id FROM label_studio_projects"))
        ).scalars()

        assert list(seen) == [1]
        # The orchestrator records the projects it creates.
        await _project(conn, lab, lab_dive, project=3)
