"""``fishsense-services-api retire-slate-labels``: dry run by default (0077;
mission 436's mirrored labels, 2026-10-26).

A run without ``--apply`` prints the plan (each label, the calibration it
withdraws, its borrowers, the measurements leaving) and its fingerprint, and
writes nothing; ``--apply <fingerprint>`` writes that plan and only that one.
As the schema owner, like the other operator tools.
"""

import re

import pytest
from sqlalchemy import text

from fishsense_services_api.cli import main
from test_slate_label_retirement import _mission_number, _rows, _seeded


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


def _args(number, labels, *more):
    return ["retire-slate-labels", "--tenant", "lab", "--mission", str(number),
            *(a for label in labels for a in ("--label", str(label))),
            "--reason", "mirrored", *more]  # fmt: skip


async def test_a_dry_run_then_the_reviewed_plan(owner_engine, as_owner, capsys):
    lab, m, labels, calibration, borrower = await _seeded(owner_engine)
    number = await _mission_number(owner_engine, m)

    assert await main(_args(number, labels)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "retire 2 slate labels (mirrored)" in out
    assert f"withdraw calibration {calibration}" in out
    assert (
        f"borrowed by missions {await _mission_number(owner_engine, borrower)}" in out
    )
    assert "1 current measurement" in out
    assert (
        await _rows(owner_engine, "SELECT 1 FROM slate_labels WHERE superseded") == []
    )
    fingerprint = re.search(r"--apply ([0-9a-f]{16})", out).group(1)

    assert await main(_args(number, labels, "--apply", "0" * 16)) == 1
    assert "nothing written" in capsys.readouterr().err
    assert (
        await _rows(owner_engine, "SELECT 1 FROM slate_labels WHERE superseded") == []
    )

    assert await main(_args(number, labels, "--apply", fingerprint)) == 0
    assert "retired 2 slate labels" in capsys.readouterr().out
    assert len(await _rows(owner_engine, "SELECT 1 FROM slate_labels WHERE "
                           "superseded_reason = 'mirrored'")) == 2  # fmt: skip


async def test_a_refused_plan_says_why(owner_engine, as_owner, capsys):
    lab, m, labels, _, _ = await _seeded(owner_engine)

    assert await main(_args(9999, labels)) == 1
    assert "no mission 9999" in capsys.readouterr().err
