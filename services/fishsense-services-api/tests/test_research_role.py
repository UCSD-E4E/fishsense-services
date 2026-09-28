"""The research read role: the lab's data, read-only, bound by RLS.

PLAN.md §9.20 is open; its lean is built here: a named research role with read
access scoped to the lab tenant -- **not** `BYPASSRLS`. v1's researchers ran
`psql -U postgres` over SSH (a superuser, so every tenant and every write);
this role is what replaces that path, pending the owner's decision.

`fishsense_research` is a NOLOGIN group role. An operator creates a login in it
(`CREATE ROLE alice LOGIN IN ROLE fishsense_research`); the login sets only its
`search_path` (`v1, public`) and runs v1's research SQL unchanged. It sees the
lab tenant's rows without setting `app.tenant_id`, and nothing else even if it
sets it to another tenant.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from depth_measure_seed import calibrated_dive, capture, tenant
from research_seed import rows
from test_v1_migration import server  # noqa: F401  (a fixture)

RESEARCH = "fishsense_research"


async def _two_tenants(owner_engine):
    """A dive and a frame in the lab and in a partner tenant."""
    seeded = {}
    for slug in ("lab", "partner"):
        tenant_id = await tenant(owner_engine, slug)
        dive_id, _ = await calibrated_dive(owner_engine, tenant_id)
        capture_id = await capture(owner_engine, tenant_id, dive_id)
        numbers = await rows(
            owner_engine,
            "SELECT (SELECT number FROM dives WHERE id = :d) AS dive, "
            "(SELECT number FROM captures WHERE id = :c) AS image",
            d=dive_id,
            c=capture_id,
        )
        seeded[slug] = (tenant_id, numbers[0])
    return seeded


async def test_the_group_role_cannot_log_in_or_bypass_rls(owner_engine):
    got = await rows(
        owner_engine,
        "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
        "FROM pg_roles WHERE rolname = :r",
        r=RESEARCH,
    )

    assert got == [
        {
            "rolcanlogin": False,
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcreaterole": False,
            "rolcreatedb": False,
        }
    ]


async def test_a_research_login_sees_the_labs_rows_without_a_tenant_setting(
    owner_engine, research_engine
):
    seeded = await _two_tenants(owner_engine)

    got = await rows(research_engine, "SELECT id FROM dive ORDER BY id")

    assert got == [{"id": seeded["lab"][1]["dive"]}]


async def test_a_research_login_cannot_see_another_tenant_by_naming_it(
    owner_engine, research_engine
):
    """`app.tenant_id` opens the canonical tenant policy for whoever sets it;
    a restrictive policy keeps a research session in the lab regardless."""
    seeded = await _two_tenants(owner_engine)
    partner = seeded["partner"][0]

    async with research_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"),
            {"t": str(partner)},
        )
        dives = (await conn.execute(text("SELECT id FROM dive"))).scalars().all()
        images = (await conn.execute(text("SELECT id FROM image"))).scalars().all()
        raw = (await conn.execute(text("SELECT id FROM public.dives"))).all()

    lab = seeded["lab"][1]
    assert dives == [lab["dive"]]
    assert images == [lab["image"]]
    assert len(raw) == 1, "the base table, read directly, is bound the same way"


async def test_without_a_lab_tenant_a_research_login_sees_nothing(
    owner_engine, research_engine
):
    """The binding names the lab by its slug; with no lab it fails closed."""
    partner = await tenant(owner_engine, "partner")
    await calibrated_dive(owner_engine, partner)

    assert await rows(research_engine, "SELECT id FROM dive") == []


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO v1.camera (serial_number, name) VALUES ('X', 'X')",
        "UPDATE v1.dive SET name = 'renamed'",
        "DELETE FROM v1.measurement",
        "INSERT INTO public.devices (tenant_id, kind, serial) "
        "VALUES (public.research_tenant_id(), 'lite', 'X')",
        "UPDATE public.dives SET name = 'renamed'",
        "DELETE FROM public.species",
        "TRUNCATE public.measurements",
    ],
)
async def test_a_research_login_cannot_write(owner_engine, research_engine, statement):
    """No write privilege anywhere; a view over a join is not even writable."""
    await tenant(owner_engine, "lab")

    with pytest.raises(
        DBAPIError, match="permission denied|not automatically updatable"
    ):
        async with research_engine.begin() as conn:
            await conn.execute(text(statement))


@pytest.mark.parametrize(
    "table", ["users", "memberships", "tenants", "measurement_refusals"]
)
async def test_a_research_login_reads_only_what_the_views_need(research_engine, table):
    with pytest.raises(DBAPIError, match="permission denied"):
        async with research_engine.connect() as conn:
            await conn.execute(text(f"SELECT 1 FROM public.{table}"))


async def test_the_app_role_is_not_given_the_research_views(app_engine):
    """The views are the research contract, not an API surface."""
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM v1.dive"))


async def test_every_research_view_is_readable_by_a_research_login(
    owner_engine, research_engine
):
    """Security-invoker views check the caller's rights on everything beneath
    them, so a missing grant anywhere fails the whole view. Each must answer."""
    await tenant(owner_engine, "lab")
    views = await rows(
        owner_engine,
        "SELECT schemaname || '.' || viewname AS name FROM pg_views "
        "WHERE schemaname = 'v1' OR viewname IN ("
        "'fish_model_measurement_accuracy', 'fish_length_estimate', "
        "'fish_model_species_mislabel_suspects') ORDER BY 1",
    )
    assert len(views) > 3

    for view in views:
        await rows(research_engine, f"SELECT * FROM {view['name']} LIMIT 1")


async def test_research_tenant_id_names_the_lab(owner_engine, research_engine):
    lab = await tenant(owner_engine, "lab")
    await tenant(owner_engine, "partner")

    got = await rows(research_engine, "SELECT public.research_tenant_id() AS t")

    assert got == [{"t": lab}]


async def test_the_research_migrations_downgrade_and_upgrade_cleanly(server, owner_url):
    """Views dropped in reverse dependency order, never CASCADE; grants and
    policies revoked; the cluster's role kept for other databases."""
    from alembic import command

    from fishsense_services_api.migrations import _config, upgrade
    from test_v1_migration import APP_ROLE, _create, _url

    url = _url(owner_url, _create(server, "research"))
    await upgrade(url, app_role=APP_ROLE)
    await asyncio.to_thread(command.downgrade, _config(url, APP_ROLE), "0028")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            left = conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM pg_namespace WHERE nspname = 'v1'),"
                    " (SELECT count(*) FROM pg_policies"
                    "  WHERE policyname LIKE 'research%'),"
                    " (SELECT count(*) FROM pg_views"
                    "  WHERE viewname LIKE 'fish\\_%'),"
                    " (SELECT count(*) FROM pg_roles WHERE rolname = :r)"
                ),
                {"r": RESEARCH},
            ).one()
    finally:
        engine.dispose()
    await upgrade(url, app_role=APP_ROLE)

    assert tuple(left) == (0, 0, 0, 1)


async def test_nobody_else_may_call_research_tenant_id(app_engine):
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT public.research_tenant_id()"))
