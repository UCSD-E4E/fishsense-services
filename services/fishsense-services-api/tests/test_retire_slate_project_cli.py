"""``fishsense-services-api retire-slate-project``: dry run by default (0079;
mission 508's pool-test slate project, 2026-10-26).

A run without ``--apply`` prints the plan (each project, its placeholders and
completed labels, the template) and the exact command that writes it, and
writes nothing; ``--apply <fingerprint>`` writes that plan and only that one.
As the schema owner, like the other operator tools.
"""

import re
import shlex

import pytest

from fishsense_services_api.cli import main
from test_slate_project_retirement import REASON, _number, _rows, _seeded


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


def _args(number, *more):
    return ["retire-slate-project", "--tenant", "lab", "--mission", str(number),
            "--reason", REASON, *more]  # fmt: skip


async def test_a_dry_run_then_the_reviewed_plan(owner_engine, as_owner, capsys):
    lab, m, _, _, _ = await _seeded(owner_engine)
    number = await _number(owner_engine, m)

    assert await main(_args(number, "--clear-template")) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "project 300745" in out and "Pool #508 - Slate" in out
    assert "3 placeholder(s) superseded" in out
    assert "0 completed" in out
    assert "cleared" in out
    assert await _rows(owner_engine, "SELECT 1 FROM label_studio_projects "
                       "WHERE retired_at IS NOT NULL") == []  # fmt: skip
    command = re.search(r"to write exactly this plan: (.+)", out).group(1)
    words = shlex.split(command)
    assert words[:1] == ["retire-slate-project"]
    assert "--clear-template" in words and REASON in words

    assert await main(_args(number, "--clear-template", "--apply", "0" * 16)) == 1
    assert "nothing written" in capsys.readouterr().err
    assert await _rows(owner_engine, "SELECT 1 FROM label_studio_projects "
                       "WHERE retired_at IS NOT NULL") == []  # fmt: skip

    assert await main(words) == 0
    out = capsys.readouterr().out
    assert "retired 1 slate project(s)" in out
    assert "superseded 3 placeholder(s)" in out
    assert "cleared the slate template" in out
    assert len(await _rows(owner_engine, "SELECT 1 FROM label_studio_projects "
                           "WHERE retired_reason = :r", r=REASON)) == 1  # fmt: skip


async def test_completed_labels_are_refused_until_allowed(
    owner_engine, as_owner, capsys
):
    lab, m, _, _, _ = await _seeded(owner_engine, completed=1)
    number = await _number(owner_engine, m)

    assert await main(_args(number)) == 1
    assert "1 completed" in capsys.readouterr().err

    assert await main(_args(number, "--allow-completed")) == 0
    out = capsys.readouterr().out
    assert "1 completed kept" in out
    assert "--allow-completed" in out


async def test_a_refused_plan_says_why(owner_engine, as_owner, capsys):
    await _seeded(owner_engine)

    assert await main(_args(9999)) == 1
    assert "no mission 9999" in capsys.readouterr().err
