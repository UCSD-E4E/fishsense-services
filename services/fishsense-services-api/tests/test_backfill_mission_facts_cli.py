"""``fishsense-services-api backfill-mission-facts``: dry run by default.

A run without ``--apply`` prints the plan and its fingerprint and writes
nothing; ``--apply <fingerprint>`` writes that plan and only that plan
(``mission_backfill``). It runs as whatever role ``FISHSENSE_DATABASE_URL``
names, under the tenant's RLS scope.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import text

from fishsense_services_api.cli import main

SAN_DIEGO = "/fishsense_data/2025.01.17.FishSense.San Diego"
CUTOFF = "2026-10-16T00:00:00+00:00"


async def _seed(owner_engine) -> str:
    async with owner_engine.begin() as conn:
        lab = (
            await conn.execute(
                text(
                    "INSERT INTO tenants (slug, name) VALUES ('lab', 'Lab') RETURNING id"
                )
            )
        ).scalar_one()
        for path, name in (
            (f"{SAN_DIEGO}/ED-00/FSL-10D/Box", "ED-00 FSL-10D Box"),
            ("dump/04-box-slate", "04-box-slate"),
        ):
            await conn.execute(
                text("""
                    INSERT INTO missions (tenant_id, source_path, name, started_at,
                                          created_at)
                    VALUES (:t, :p, :n, :s, :c)
                    """),
                {
                    "t": lab,
                    "p": path,
                    "n": name,
                    "s": datetime(2025, 1, 17, tzinfo=timezone.utc),
                    "c": datetime(2026, 10, 1, tzinfo=timezone.utc),
                },
            )
    return str(lab)


async def _facts(owner_engine):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT name, port, purpose FROM missions ORDER BY number")
            )
        ).all()


def _args(lab, *more):
    return [
        "backfill-mission-facts",
        "--tenant",
        lab,
        "--ingested-before",
        CUTOFF,
        *more,
    ]


async def test_a_dry_run_prints_the_plan_and_writes_nothing(
    owner_engine, app_url, monkeypatch, capsys
):
    lab = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(_args(lab)) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "2025.01.17.FishSense.San Diego" in out
    assert "device_suffix" in out
    assert "the name says both known_length and slate" in out
    assert re.search(r"--apply [0-9a-f]{16}", out)
    assert {(r.port, r.purpose) for r in await _facts(owner_engine)} == {
        ("unknown", None)
    }


async def test_apply_writes_the_reviewed_plan(
    owner_engine, app_url, monkeypatch, capsys
):
    lab = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    async with owner_engine.connect() as conn:
        ambiguous = (
            await conn.execute(
                text("SELECT number FROM missions WHERE name = '04-box-slate'")
            )
        ).scalar_one()
    override = f"{ambiguous}=slate"
    await main(_args(lab, "--purpose", override))
    fingerprint = re.search(r"--apply ([0-9a-f]{16})", capsys.readouterr().out)[1]

    assert await main(_args(lab, "--purpose", override, "--apply", fingerprint)) == 0

    assert [(r.port, r.purpose) for r in await _facts(owner_engine)] == [
        ("corrective_optic", "known_length"),
        ("corrective_optic", "slate"),
    ]
    assert "applied" in capsys.readouterr().out


async def test_apply_refuses_a_plan_nobody_reviewed(
    owner_engine, app_url, monkeypatch, capsys
):
    lab = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(_args(lab, "--apply", "0123456789abcdef")) == 1

    assert "review it again" in capsys.readouterr().err
    assert {r.port for r in await _facts(owner_engine)} == {"unknown"}


async def test_a_bad_override_is_refused_before_anything_runs(monkeypatch, capsys):
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", "postgresql+asyncpg://nowhere/x")

    assert await main(_args("lab", "--purpose", "2=fishing")) == 2
    assert await main(_args("lab", "--purpose", "two=slate")) == 2
    assert "--purpose" in capsys.readouterr().err
