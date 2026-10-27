"""``fishsense-services-api assign-slate-templates``: dry run by default (the
owner, 2026-10-26: template choices for missions with none go into mission
metadata).

A run without ``--apply`` prints the plan (assignments, unchanged missions,
conflicts, and the Label Studio and calibration work they set off) and its
fingerprint, and writes nothing; ``--apply <fingerprint>`` writes that plan
and only that one. As the schema owner, like the other operator tools.
"""

import json
import re

import pytest

from fishsense_services_api.cli import main
from test_slate_template_assignment import _setup, _templates


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


def _args(path, *more):
    return ["assign-slate-templates", "--tenant", "lab", "--from", str(path), *more]


async def test_a_dry_run_then_the_reviewed_plan(owner_engine, as_owner, capsys,
                                                tmp_path):  # fmt: skip
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping))
    before = await _templates(owner_engine)

    assert await main(_args(path)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "assign 4 missions" in out
    assert f"mission {n['plain']}: " in out
    assert f"unchanged: {n['same']}" in out
    assert f"conflict: mission {n['other']} is on " in out
    assert "stage 9 (slate preprocessing, new Label Studio work): 1 mission" in out
    assert f"mission {n['marked']}: 1 frame" in out
    assert f"stage 13 (laser calibration) entering: {n['labelled']}" in out
    assert f"checkerboard calibration entering: {n['refused']}" in out
    assert await _templates(owner_engine) == before
    fingerprint = re.search(r"--apply ([0-9a-f]{16})", out).group(1)

    assert await main(_args(path, "--apply", "0" * 16)) == 1
    assert "nothing written" in capsys.readouterr().err
    assert await _templates(owner_engine) == before

    assert await main(_args(path, "--apply", fingerprint)) == 0
    assert "set 4 missions' slate templates" in capsys.readouterr().out
    after = await _templates(owner_engine)
    assert after[ids["marked"]][0] == a and after[ids["plain"]][0] == b


async def test_a_refused_mapping_says_why(owner_engine, as_owner, capsys, tmp_path):
    lab, a, b, ids, n, mapping = await _setup(owner_engine)
    mapping["missions"]["9999"] = {"template_v1": 7}
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping))

    assert await main(_args(path)) == 1
    assert "no mission 9999" in capsys.readouterr().err


async def test_an_unreadable_mapping_says_why(owner_engine, as_owner, capsys,
                                              tmp_path):  # fmt: skip
    await _setup(owner_engine)
    path = tmp_path / "mapping.json"
    path.write_text("{not json")

    assert await main(_args(path)) == 2
    assert "mapping.json" in capsys.readouterr().err
