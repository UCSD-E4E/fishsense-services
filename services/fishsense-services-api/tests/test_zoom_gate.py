"""Withdrawing the automatic lengths already on zoomed frames (the owner,
2026-10-25).

When the focal lengths were recorded, 152 automatic lengths sat on frames
shot zoomed in: their depth and length assume intrinsics the frame was not
shot at. Automatic results are append-only, so a length is withdrawn by
appending a refusal (``zoomed``) that becomes the frame's current row; the
length stays in the history. Pinned here:

* only a current length on a zoomed frame is listed -- not one on a wide or
  unknown frame, not one already withdrawn or refused;
* the withdrawal writes exactly the listed rows, each a refusal carrying the
  length's provenance, and the frame then has no current length;
* the CLI is a dry run unless told otherwise, and an apply that would write
  other than what it listed writes nothing.
"""

import uuid

import pytest
from sqlalchemy import text

from _slate_calibration_seed import zoom
from depth_measure_seed import capture, forget_identities, tenant  # noqa: F401
from fishsense_services_api.cli import main
from fishsense_services_api.zoom_gate import (
    withdraw_zoomed_automatic_lengths,
    zoomed_automatic_lengths,
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


async def test_only_a_current_length_on_a_zoomed_frame_is_listed(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, zoomed = await _measured(owner_engine, app_engine, lab)
    _, wide = await _measured(owner_engine, app_engine, lab)
    _, unknown = await _measured(owner_engine, app_engine, lab)
    await zoom(owner_engine, zoomed, 18.0)
    await zoom(owner_engine, wide, 4.5)

    async with owner_engine.connect() as conn:
        listed = await zoomed_automatic_lengths(conn)

    assert [(z.capture_id, z.length_m, z.focal_length_mm) for z in listed] == [
        (zoomed, 0.42, 18.0)
    ]
    assert unknown not in {z.capture_id for z in listed}


async def test_the_withdrawal_appends_a_zoomed_refusal_and_keeps_the_history(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    await zoom(owner_engine, c, 14.62)
    async with owner_engine.connect() as conn:
        listed = await zoomed_automatic_lengths(conn)

    async with owner_engine.begin() as conn:
        written = await withdraw_zoomed_automatic_lengths(
            conn, [z.measurement_id for z in listed]
        )

    assert written == 1
    current = await _current(owner_engine, c)
    assert (current.length_m, current.refusal, current.depth_m) == (
        None,
        "zoomed",
        None,
    )
    assert current.algorithm_version == "1"  # the length's provenance
    assert await _history(owner_engine, c) == 2
    async with owner_engine.connect() as conn:
        assert await zoomed_automatic_lengths(conn) == []


async def test_a_length_no_longer_current_or_no_longer_zoomed_is_not_withdrawn(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    await zoom(owner_engine, c, 18.0)
    async with owner_engine.connect() as conn:
        (listed,) = await zoomed_automatic_lengths(conn)
    await zoom(owner_engine, c, 4.5)  # re-read meanwhile

    async with owner_engine.begin() as conn:
        assert (
            await withdraw_zoomed_automatic_lengths(conn, [listed.measurement_id]) == 0
        )
        assert await withdraw_zoomed_automatic_lengths(conn, [uuid.uuid4()]) == 0


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


async def test_the_cli_is_a_dry_run_unless_told(owner_engine, app_engine, as_owner,
                                                capsys):  # fmt: skip
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    await zoom(owner_engine, c, 18.0)

    assert await main(["withdraw-zoomed-lengths"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "1 automatic length" in out
    assert (await _current(owner_engine, c)).length_m == 0.42

    assert await main(["withdraw-zoomed-lengths", "--apply"]) == 0
    assert (await _current(owner_engine, c)).refusal == "zoomed"
    assert await main(["withdraw-zoomed-lengths"]) == 0
    assert "nothing to withdraw" in capsys.readouterr().out


async def test_only_the_named_lengths_are_withdrawn(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    _, first = await _measured(owner_engine, app_engine, lab)
    _, second = await _measured(owner_engine, app_engine, lab)
    for c in (first, second):
        await zoom(owner_engine, c, 18.0)
    async with owner_engine.connect() as conn:
        listed = {
            z.capture_id: z.measurement_id for z in await zoomed_automatic_lengths(conn)
        }

    async with owner_engine.begin() as conn:
        assert await withdraw_zoomed_automatic_lengths(conn, [listed[first]]) == 1

    assert (await _current(owner_engine, first)).refusal == "zoomed"
    assert (await _current(owner_engine, second)).length_m == 0.42


async def test_an_apply_that_writes_other_than_it_listed_writes_nothing(
    owner_engine, app_engine, as_owner, monkeypatch
):
    """Listed and written in one transaction: a short count rolls it back."""
    from fishsense_services_api import cli

    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)
    await zoom(owner_engine, c, 18.0)

    async def short(conn, ids, *reason):
        return await withdraw_zoomed_automatic_lengths(conn, ids, *reason) - 1

    monkeypatch.setattr(cli, "withdraw_zoomed_automatic_lengths", short)

    with pytest.raises(RuntimeError, match="wrote 0 of 1"):
        await main(["withdraw-zoomed-lengths", "--apply"])
    assert (await _current(owner_engine, c)).length_m == 0.42
