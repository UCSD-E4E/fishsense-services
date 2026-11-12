"""``move-missions`` (the owner, 2026-11-11): a dry run that prints the
move and its fingerprint, then ``--apply <fingerprint>`` writes exactly that
move, as the schema owner. Missions come by number, on the command line or
one per line in a file; buckets default to ``fishsense-tenant-<slug>``.
Pinned here:

* the dry run writes nothing and prints the missions, the cameras moving and
  split, the rows by table, and the command that applies it;
* the apply with that fingerprint moves them; with another, nothing;
* an unknown tenant, or a mission the source doesn't have, writes nothing.
"""

import re

import pytest
from sqlalchemy import text

from fishsense_services_api.cli import main
from test_tenant_move import _rows, _seeded


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


def _fingerprint(out: str) -> str:
    return re.search(r"--apply ([0-9a-f]{16})", out).group(1)


async def test_a_dry_run_then_the_apply(owner_engine, as_owner, capsys, tmp_path):
    seed = await _seeded(owner_engine)
    listed = tmp_path / "reef.txt"
    listed.write_text(f"{seed['numbers'][0]}\n# a comment\n\n{seed['numbers'][1]}\n")

    assert await main(["move-missions", "--from", "lab", "--to", "reef",
                       "--missions-file", str(listed)]) == 0  # fmt: skip
    out = capsys.readouterr().out

    assert "DRY RUN: nothing written" in out
    assert "2 missions" in out
    assert "FSL-01" in out and "FSL-02" in out
    assert re.search(r"captures\s+2", out)
    assert (await _rows(owner_engine, seed["reef"]))["missions"] == 0

    fingerprint = _fingerprint(out)
    assert await main(["move-missions", "--from", "lab", "--to", "reef",
                       "--missions-file", str(listed), "--apply", "0" * 16]) == 1  # fmt: skip
    assert (await _rows(owner_engine, seed["reef"]))["missions"] == 0

    assert await main(["move-missions", "--from", "lab", "--to", "reef",
                       "--missions-file", str(listed), "--apply", fingerprint]) == 0  # fmt: skip
    assert (await _rows(owner_engine, seed["reef"]))["missions"] == 2
    async with owner_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM missions m "
                "JOIN tenants t ON t.id = m.tenant_id WHERE t.slug = 'lab'"))).scalar_one() == 1  # fmt: skip


async def test_refusals_write_nothing(owner_engine, as_owner, capsys):
    seed = await _seeded(owner_engine)
    one = str(seed["numbers"][0])

    assert await main(["move-missions", "--from", "lab", "--to", "nowhere",
                       "--mission", one]) == 1  # fmt: skip
    assert await main(["move-missions", "--from", "lab", "--to", "reef",
                       "--mission", one, "--mission", "999999"]) == 1  # fmt: skip
    assert "999999" in capsys.readouterr().err
    assert (await _rows(owner_engine, seed["reef"]))["missions"] == 0
