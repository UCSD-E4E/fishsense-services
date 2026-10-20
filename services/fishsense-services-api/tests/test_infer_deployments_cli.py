"""``fishsense-services-api infer-deployments``: dry run by default.

A run without ``--apply`` prints the trips and the fingerprint and writes
nothing; ``--apply <fingerprint>`` writes that plan and only that plan
(``deployment_inference``). It runs as whatever role ``FISHSENSE_DATABASE_URL``
names, under the tenant's RLS scope.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fishsense_services_api import cli
from fishsense_services_api.cli import main

SMILE = "2025-02-10 REEF Data Dump SMILE 6"


async def _seed(owner_engine) -> tuple[str, dict[str, int]]:
    async with owner_engine.begin() as conn:
        lab = (
            await conn.execute(
                text(
                    "INSERT INTO tenants (slug, name) VALUES ('lab', 'Lab') RETURNING id"
                )
            )
        ).scalar_one()
        devices = {}
        for serial in ("FSL-01", "FSL-05"):
            devices[serial] = (
                await conn.execute(
                    text(
                        "INSERT INTO devices (tenant_id, kind, serial) "
                        "VALUES (:t, 'lite', :s) RETURNING id"
                    ),
                    {"t": lab, "s": serial},
                )
            ).scalar_one()
        numbers = {}
        for key, path, serial in (
            ("july", f"{SMILE}/07_2024_FSL01/070924_Dive1_FSL01", "FSL-01"),
            ("misfiled", f"{SMILE}/08_20_24_Alligator_FSL05/082024_Dive1_fix site",
             "FSL-05"),  # fmt: skip
        ):
            at = datetime(2024, 7, 9, 21, tzinfo=timezone.utc)
            mission_id, numbers[key] = (
                await conn.execute(
                    text("""
                        INSERT INTO missions (tenant_id, source_path, name,
                                              started_at, device_id)
                        VALUES (:t, :p, :p, :at, :d) RETURNING id, number
                        """),
                    {"t": lab, "p": path, "at": at, "d": devices[serial]},
                )
            ).one()
            for i in range(2):
                await conn.execute(
                    text("""
                        INSERT INTO captures (tenant_id, mission_id, device_id,
                                              source_path, captured_at, checksum)
                        VALUES (:t, :m, :d, :p, :at, :s)
                        """),
                    {"t": lab, "m": mission_id, "d": devices[serial],
                     "p": f"{path}/P{i}.ORF", "at": at + timedelta(minutes=i),
                     "s": uuid.uuid4().hex},
                )  # fmt: skip
    return str(lab), numbers


async def _deployments(owner_engine):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text("""
                    SELECT d.name FROM missions m
                    LEFT JOIN deployments d ON d.id = m.deployment_id
                    ORDER BY m.number
                    """))).scalars().all()


async def test_a_dry_run_prints_the_trips_and_writes_nothing(
    owner_engine, app_url, monkeypatch, capsys
):
    lab, _ = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(["infer-deployments", "--tenant", lab]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "2024.07.09.FishSense" in out
    assert "2024.08.20.FishSense.Islamorada" in out
    assert re.search(r"--apply [0-9a-f]{16}", out)
    assert await _deployments(owner_engine) == [None, None]


async def test_apply_writes_the_reviewed_plan_with_the_operators_choices(
    owner_engine, app_url, monkeypatch, capsys
):
    lab, n = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    choice = f"{n['misfiled']}={n['july']}"
    await main(["infer-deployments", "--tenant", lab, "--together", choice])
    out = capsys.readouterr().out
    assert f"put with mission {n['july']} by the operator" in out
    fingerprint = re.search(r"--apply ([0-9a-f]{16})", out)[1]

    assert (
        await main(["infer-deployments", "--tenant", lab, "--together", choice,
                    "--apply", fingerprint])  # fmt: skip
        == 0
    )

    assert await _deployments(owner_engine) == ["2024.07.09.FishSense"] * 2
    assert "applied" in capsys.readouterr().out


async def test_apply_refuses_a_plan_nobody_reviewed(
    owner_engine, app_url, monkeypatch, capsys
):
    lab, _ = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(["infer-deployments", "--tenant", lab, "--apply", "0" * 16]) == 1

    assert "nothing written" in capsys.readouterr().err
    assert await _deployments(owner_engine) == [None, None]


async def test_a_malformed_choice_is_refused(
    owner_engine, app_url, monkeypatch, capsys
):
    lab, _ = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(["infer-deployments", "--tenant", lab, "--together", "x"]) == 2


async def test_a_mission_chosen_twice_is_refused(
    owner_engine, app_url, monkeypatch, capsys
):
    lab, n = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    m, j = n["misfiled"], n["july"]

    assert (
        await main(["infer-deployments", "--tenant", lab, "--together", f"{m}={j}",
                    "--together", f"{m}={m}"])  # fmt: skip
        == 2
    )
    assert "twice" in capsys.readouterr().err


async def test_a_negative_gap_is_refused(owner_engine, app_url, monkeypatch, capsys):
    lab, _ = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    with pytest.raises(SystemExit):
        await main(["infer-deployments", "--tenant", lab, "--gap-days", "-1"])


async def test_a_database_refusal_on_apply_is_nothing_written(
    owner_engine, app_url, monkeypatch, capsys
):
    """A deployment created between the locked re-plan and the INSERT."""
    lab, _ = await _seed(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    async def collides(*args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("deployments_tenant_id_name_key"))

    monkeypatch.setattr(cli, "apply_inference", collides)

    assert await main(["infer-deployments", "--tenant", lab, "--apply", "0" * 16]) == 1
    assert "nothing written" in capsys.readouterr().err
