"""``fishsense-services-api system-admin``: an operator grants or revokes a
system admin (0056; the owner, 2026-10-20: a flag in our database).

A dry run by default; ``--apply`` writes, guarded on the old value, as the
schema owner. A user is named by their sign-in subject, and must have signed
in once.
"""

import pytest
from sqlalchemy import text

from fishsense_services_api.cli import main


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


@pytest.fixture
async def users(seed_memberships):
    return await seed_memberships({"ops-sub": {"lab": "member"}})


async def _flag(owner_engine) -> bool:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT is_system_admin FROM users WHERE sub = 'ops-sub'")
            )
        ).scalar_one()


async def test_a_dry_run_says_what_it_would_do_and_writes_nothing(
    as_owner, owner_engine, users, capsys
):
    assert await main(["system-admin", "--user", "ops-sub", "--grant"]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out and "ops-sub" in out
    assert await _flag(owner_engine) is False


async def test_apply_grants_and_revokes(as_owner, owner_engine, users):
    assert await main(["system-admin", "--user", "ops-sub", "--grant", "--apply"]) == 0
    assert await _flag(owner_engine) is True

    assert await main(["system-admin", "--user", "ops-sub", "--revoke", "--apply"]) == 0
    assert await _flag(owner_engine) is False


async def test_granting_twice_changes_nothing(as_owner, owner_engine, users, capsys):
    await main(["system-admin", "--user", "ops-sub", "--grant", "--apply"])
    capsys.readouterr()

    assert await main(["system-admin", "--user", "ops-sub", "--grant", "--apply"]) == 1
    assert "already" in capsys.readouterr().err


async def test_someone_who_never_signed_in_is_refused(as_owner, users, capsys):
    assert await main(["system-admin", "--user", "nobody", "--grant"]) == 1
    assert "has not signed in" in capsys.readouterr().err
