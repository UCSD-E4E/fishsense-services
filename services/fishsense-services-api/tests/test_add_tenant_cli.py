"""``fishsense-services-api add-tenant``: an operator opens a partner's tenant.

A partner org gets its own tenant, and the tenant claims the org's ``org``
claim (migration 0037), so the people who enroll through the org's invite
(krg-infra ``collaborator_invites.tf``) join it as members by themselves.
Creating a tenant and pointing an org at it are administrative: this runs as
the schema owner, never through the API. Re-running is safe -- it converges.
"""

import pytest
from sqlalchemy import text

from fishsense_services_api.cli import main


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


async def _tenants(owner_engine) -> list[tuple]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT slug, name, org_claim FROM tenants ORDER BY slug")
        )
        return [tuple(row) for row in rows]


async def test_it_creates_a_tenant_that_claims_an_org(as_owner, owner_engine, capsys):
    argv = ["add-tenant", "conservation-angler", "--name", "Conservation Angler",
            "--org-claim", "conservation-angler"]  # fmt: skip

    assert await main(argv) == 0
    assert await _tenants(owner_engine) == [
        ("conservation-angler", "Conservation Angler", "conservation-angler")
    ]
    assert "conservation-angler" in capsys.readouterr().out


async def test_a_tenant_need_not_claim_an_org(as_owner, owner_engine):
    assert await main(["add-tenant", "staging", "--name", "Staging"]) == 0
    assert await _tenants(owner_engine) == [("staging", "Staging", None)]


async def test_rerunning_converges_on_the_latest_name_and_claim(as_owner, owner_engine):
    await main(["add-tenant", "ca", "--name", "CA"])
    await main(["add-tenant", "ca", "--name", "Conservation Angler",
                "--org-claim", "conservation-angler"])  # fmt: skip
    await main(["add-tenant", "ca", "--name", "Conservation Angler"])

    assert await _tenants(owner_engine) == [
        ("ca", "Conservation Angler", "conservation-angler")
    ]


async def test_an_org_already_claimed_elsewhere_is_refused(
    as_owner, owner_engine, capsys
):
    await main(["add-tenant", "first", "--name", "First", "--org-claim", "org-a"])
    capsys.readouterr()

    assert await main(["add-tenant", "second", "--name", "Second",
                       "--org-claim", "org-a"]) == 1  # fmt: skip
    assert "org-a" in capsys.readouterr().err
    assert await _tenants(owner_engine) == [("first", "First", "org-a")]


@pytest.mark.parametrize("slug", ["Lab", "lab_1", "a/b", "a b", ""])
async def test_a_slug_that_cannot_name_a_path_is_refused(
    as_owner, owner_engine, slug, capsys
):
    assert await main(["add-tenant", slug, "--name", "x"]) == 2
    assert await _tenants(owner_engine) == []


async def test_without_an_owner_dsn_it_fails_naming_it(monkeypatch, capsys):
    monkeypatch.delenv("FISHSENSE_MIGRATION_DATABASE_URL", raising=False)

    assert await main(["add-tenant", "x", "--name", "x"]) == 2
    assert "FISHSENSE_MIGRATION_DATABASE_URL" in capsys.readouterr().err
