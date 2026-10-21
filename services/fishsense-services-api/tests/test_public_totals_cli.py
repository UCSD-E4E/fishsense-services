"""``fishsense-services-api public-totals``: an operator opts a tenant in, or
out, of the signed-out page (0055; the owner, 2026-10-20).

A dry run by default: it shows what the public page would say with the change,
and writes nothing. ``--apply`` writes it, guarded on the old value, as the
schema owner (like ``add-tenant``).
"""

import pytest
from depth_measure_seed import exec_, forget_identities  # noqa: F401
from observation_seed import ALICE, lite
from sqlalchemy import text

from fishsense_services_api.cli import main


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member"}})


async def _flag(owner_engine) -> bool:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT public_totals FROM tenants WHERE slug = 'lab'")
            )
        ).scalar_one()


async def _seed(owner_engine, tenant_id):
    capture_id, _ = await lite(
        owner_engine, tenant_id, species=("Scarus coeruleus", "Blue")
    )
    await exec_(
        owner_engine,
        "UPDATE missions SET source_path = 'SMILE/Molasses/1' "
        "WHERE id = (SELECT mission_id FROM captures WHERE id = :c)",
        c=capture_id,
    )


async def test_a_dry_run_shows_the_page_as_it_would_read_and_writes_nothing(
    as_owner, owner_engine, tenants, capsys
):
    await _seed(owner_engine, tenants["lab"])

    assert await main(["public-totals", "--tenant", "lab", "--enable"]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "1 wild fish" in out
    assert "Florida Keys, USA: under 25" in out
    assert await _flag(owner_engine) is False


async def test_apply_opts_the_tenant_in(as_owner, owner_engine, tenants, capsys):
    assert await main(["public-totals", "--tenant", "lab", "--enable", "--apply"]) == 0

    assert await _flag(owner_engine) is True
    assert "lab now counts" in capsys.readouterr().out


async def test_apply_twice_changes_nothing_the_second_time(
    as_owner, owner_engine, tenants, capsys
):
    await main(["public-totals", "--tenant", "lab", "--enable", "--apply"])
    capsys.readouterr()

    assert await main(["public-totals", "--tenant", "lab", "--enable", "--apply"]) == 1
    assert "already" in capsys.readouterr().err


async def test_disable_opts_it_back_out(as_owner, owner_engine, tenants):
    await main(["public-totals", "--tenant", "lab", "--enable", "--apply"])

    assert await main(["public-totals", "--tenant", "lab", "--disable", "--apply"]) == 0
    assert await _flag(owner_engine) is False


async def test_an_unknown_tenant_is_refused(as_owner, tenants, capsys):
    assert await main(["public-totals", "--tenant", "nobody", "--enable"]) == 1
    assert "unknown tenant" in capsys.readouterr().err
