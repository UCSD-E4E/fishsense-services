"""The orchestrator serves every tenant by default (the owner, 2026-10-21:
"the orchestrator is responsible for running everything").

It still works in a tenant only as a member of it (PLAN.md §9.11), so the
default is a membership, never a bypass: ``add-tenant`` makes it a member of
the tenant it opens, and ``serve-tenants`` gives it the tenants opened before.
Revoking one membership still stops the pipeline for that tenant. Its subject
is ``FISHSENSE_ORCHESTRATOR_SUB``, the orchestrator's own setting.
"""

import pytest
from sqlalchemy import text

from fishsense_services_api.cli import main

ORCHESTRATOR = "service:fishsense-orchestrator"


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)
    monkeypatch.setenv("FISHSENSE_ORCHESTRATOR_SUB", ORCHESTRATOR)


async def _served(owner_engine) -> list[tuple[str, str]]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("""
                SELECT t.slug, m.role FROM memberships m
                JOIN users u ON u.id = m.user_id JOIN tenants t ON t.id = m.tenant_id
                WHERE u.sub = :s ORDER BY t.slug
                """),
            {"s": ORCHESTRATOR},
        )
        return [tuple(r) for r in rows]


async def _tenant(owner_engine, slug):
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (slug, name) VALUES (:s, :s)"), {"s": slug}
        )


async def test_a_new_tenant_is_served_from_the_start(as_owner, owner_engine):
    assert await main(["add-tenant", "ccfrp-ucsd", "--name", "CCFRP UCSD"]) == 0

    assert await _served(owner_engine) == [("ccfrp-ucsd", "member")]


async def test_opening_a_tenant_again_changes_nothing(as_owner, owner_engine):
    await main(["add-tenant", "ccfrp-ucsd", "--name", "CCFRP UCSD"])

    assert await main(["add-tenant", "ccfrp-ucsd", "--name", "CCFRP"]) == 0
    assert await _served(owner_engine) == [("ccfrp-ucsd", "member")]


async def test_without_the_orchestrators_subject_no_tenant_is_opened(
    owner_url, owner_engine, monkeypatch, capsys
):
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)
    monkeypatch.delenv("FISHSENSE_ORCHESTRATOR_SUB", raising=False)

    assert await main(["add-tenant", "ccfrp-ucsd", "--name", "CCFRP UCSD"]) == 2
    assert "FISHSENSE_ORCHESTRATOR_SUB" in capsys.readouterr().err
    async with owner_engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM tenants"))
        ).scalar_one() == 0


async def test_a_dry_run_names_the_tenants_it_would_serve_and_writes_nothing(
    as_owner, owner_engine, capsys
):
    for slug in ("reef", "conservation-angler"):
        await _tenant(owner_engine, slug)

    assert await main(["serve-tenants"]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out and "conservation-angler" in out and "reef" in out
    assert await _served(owner_engine) == []


async def test_apply_serves_every_tenant_not_yet_served(as_owner, owner_engine):
    await main(["add-tenant", "lab", "--name", "Lab"])
    for slug in ("reef", "conservation-angler"):
        await _tenant(owner_engine, slug)

    assert await main(["serve-tenants", "--apply"]) == 0

    assert await _served(owner_engine) == [
        ("conservation-angler", "member"),
        ("lab", "member"),
        ("reef", "member"),
    ]


async def test_with_every_tenant_served_there_is_nothing_to_do(
    as_owner, owner_engine, capsys
):
    await main(["add-tenant", "lab", "--name", "Lab"])
    capsys.readouterr()

    assert await main(["serve-tenants", "--apply"]) == 0
    assert "every tenant is served" in capsys.readouterr().out
