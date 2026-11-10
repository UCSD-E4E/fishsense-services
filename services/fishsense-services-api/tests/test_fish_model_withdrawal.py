"""Withdrawing the automatic lengths of a pool trial's fish models (the owner,
2026-11-10).

The lab's automatic map showed 11 fish in Southern California: automatic
lengths on frames of mission 509 (Nathan's Pool, 2023-08-18), a pool trial
whose 162 labeled fish are all fish models. Nobody labeled those 11 frames,
so nothing said they were decoys. Automatic results are append-only, so a
length is withdrawn by appending a refusal (``fish_model``) that becomes the
frame's current row; the length stays in the history. Pinned here:

* only a current automatic length in a named mission, on a frame no person
  measured, is listed: a frame a person measured keeps its automatic length
  (against a fish model's known length it is a control), and another
  mission's is not touched;
* the withdrawal appends a ``fish_model`` refusal carrying the length's
  provenance, and keeps the history;
* the CLI is a dry run unless told otherwise, and an apply that would write
  other than what it listed writes nothing.
"""

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401
    calibrate,
    fish,
    forget_identities,
    measurement,
    tenant,
)
from fishsense_services_api.cli import main
from fishsense_services_api.fish_model_withdrawal import (
    fish_model_automatic_lengths,
    withdraw_fish_model_lengths,
)

from test_automatic_results_store import _measured

pytestmark = pytest.mark.usefixtures("forget_identities")


async def _current(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("""
                    SELECT length_m, refusal, depth_m, algorithm_version
                    FROM current_automatic_measurements WHERE capture_id = :c
                    """),
                {"c": capture_id},
            )
        ).one_or_none()


async def _number(owner_engine, mission_id) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT number FROM missions WHERE id = :m"), {"m": mission_id}
            )
        ).scalar_one()


async def _history(owner_engine, capture_id) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM automatic_measurements WHERE capture_id = :c"
                ),
                {"c": capture_id},
            )
        ).scalar_one()


async def test_only_an_unlabeled_frames_current_length_in_a_named_mission_is_listed(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    pool, unlabeled = await _measured(owner_engine, app_engine, lab)
    trial, labeled = await _measured(owner_engine, app_engine, lab)
    _, elsewhere = await _measured(owner_engine, app_engine, lab)
    cal = await calibrate(owner_engine, lab, trial)
    await measurement(
        owner_engine, lab, labeled, await fish(owner_engine, lab, model="George"), cal
    )
    numbers = [await _number(owner_engine, m) for m in (pool, trial)]

    async with owner_engine.connect() as conn:
        listed = await fish_model_automatic_lengths(conn, lab, numbers)

    assert [(z.capture_id, z.length_m) for z in listed] == [(unlabeled, 0.42)]
    assert elsewhere not in {z.capture_id for z in listed}


async def test_the_withdrawal_appends_a_fish_model_refusal_and_keeps_the_history(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    pool, c = await _measured(owner_engine, app_engine, lab)
    number = await _number(owner_engine, pool)
    async with owner_engine.connect() as conn:
        listed = await fish_model_automatic_lengths(conn, lab, [number])

    async with owner_engine.begin() as conn:
        written = await withdraw_fish_model_lengths(
            conn, lab, [number], [z.measurement_id for z in listed]
        )

    assert written == 1
    current = await _current(owner_engine, c)
    assert (current.length_m, current.refusal, current.depth_m) == (
        None,
        "fish_model",
        None,
    )
    assert current.algorithm_version == "1"  # the length's provenance
    assert await _history(owner_engine, c) == 2
    async with owner_engine.connect() as conn:
        assert await fish_model_automatic_lengths(conn, lab, [number]) == []


async def test_a_length_outside_the_named_missions_is_not_withdrawn(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    pool, _ = await _measured(owner_engine, app_engine, lab)
    other, c = await _measured(owner_engine, app_engine, lab)
    async with owner_engine.connect() as conn:
        (listed,) = await fish_model_automatic_lengths(
            conn, lab, [await _number(owner_engine, other)]
        )

    async with owner_engine.begin() as conn:
        assert (
            await withdraw_fish_model_lengths(
                conn, lab, [await _number(owner_engine, pool)], [listed.measurement_id]
            )
            == 0
        )
    assert (await _current(owner_engine, c)).length_m == 0.42


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


async def _slug(owner_engine, tenant_id) -> str:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id}
            )
        ).scalar_one()


async def test_the_cli_is_a_dry_run_unless_told(
    owner_engine, app_engine, as_owner, capsys
):
    lab = await tenant(owner_engine)
    pool, c = await _measured(owner_engine, app_engine, lab)
    args = ["withdraw-fish-model-lengths", "--tenant", await _slug(owner_engine, lab),
            "--missions", str(await _number(owner_engine, pool))]  # fmt: skip

    assert await main(args) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "1 automatic length" in out and str(c) in out
    assert (await _current(owner_engine, c)).length_m == 0.42

    assert await main([*args, "--apply"]) == 0
    assert (await _current(owner_engine, c)).refusal == "fish_model"
    assert await main(args) == 0
    assert "nothing to withdraw" in capsys.readouterr().out


async def test_an_unknown_tenant_is_refused(as_owner, capsys):
    assert await main(["withdraw-fish-model-lengths", "--tenant", "nobody",
                       "--missions", "1"]) == 1  # fmt: skip
    assert "unknown tenant" in capsys.readouterr().err


async def test_an_apply_that_writes_other_than_it_listed_writes_nothing(
    owner_engine, app_engine, as_owner, monkeypatch
):
    """Listed and written in one transaction: a short count rolls it back."""
    from fishsense_services_api import cli

    lab = await tenant(owner_engine)
    pool, c = await _measured(owner_engine, app_engine, lab)

    async def short(conn, *args):
        return await withdraw_fish_model_lengths(conn, *args) - 1

    monkeypatch.setattr(cli, "withdraw_fish_model_lengths", short)

    with pytest.raises(RuntimeError, match="wrote 0 of 1"):
        await main(["withdraw-fish-model-lengths", "--tenant",
                    await _slug(owner_engine, lab), "--missions",
                    str(await _number(owner_engine, pool)), "--apply"])  # fmt: skip
    assert (await _current(owner_engine, c)).length_m == 0.42
