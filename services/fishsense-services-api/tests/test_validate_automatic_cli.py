"""``fishsense-services-api validate-automatic``: the harness on a database.

New in v2. Reads, never writes: per paper dive (by number, in a tenant), the
frames humans labelled (a valid laser label and a valid head/tail label, the
lowest-numbered of each, as stage 14 takes them), their species content (the
pool's fish models and ruler, with their tape lengths from
`current_fish_model_references`; the reef's anything but a slate), the dive's
effective stored calibration and its automatic one, and the automatic chain's
current output for each frame. Automatic outputs may instead come from a file
(`--frame-outputs`, one JSON object per frame keyed by capture number) --
how the paper's own GPU outputs are scored through v2's code.
"""

from __future__ import annotations

import json

import pytest

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrate,
    capture,
    device,
    dive,
    exec_,
    forget_identities,
    head_tail_label,
    laser_label,
    species_label,
    tenant,
)
from fishsense_services_api.automatic_results_store import (
    AutomaticHeadTailRow,
    persist_automatic_head_tails,
)
from fishsense_services_api.automatic_results_store import (
    AutomaticCalibrationRow,
    persist_automatic_calibration,
)
from fishsense_services_api.automatic_validation_store import (
    session_calibrations,
    validation_frames,
)
from fishsense_services_api.cli import main
from fishsense_services_api.db import tenant_transaction
from research_seed import references  # noqa: F401  (a fixture)


async def _paper_dive(owner_engine, lab, number):
    device_id = await device(owner_engine, lab)
    dive_id = await dive(owner_engine, lab, device_id=device_id)
    await exec_(owner_engine, "UPDATE dives SET number = :n WHERE id = :d",
                n=number, d=dive_id)  # fmt: skip
    await calibrate(owner_engine, lab, dive_id)
    return dive_id


async def _labelled(owner_engine, lab, dive_id, content, *, green=False):
    c = await capture(owner_engine, lab, dive_id)
    label = await laser_label(owner_engine, lab, c, x=2048.0, y=1600.0)
    if green:
        await exec_(owner_engine, "UPDATE laser_labels SET label = 'Green Laser' "
                    "WHERE id = :l", l=label)  # fmt: skip
    await head_tail_label(owner_engine, lab, c, head=(1900.0, 1600.0),
                          tail=(2200.0, 1600.0))  # fmt: skip
    if content:
        await species_label(owner_engine, lab, c, content)
    return c


async def _auto(app_engine, lab, dive_id, c):
    async with tenant_transaction(app_engine, lab) as conn:
        await persist_automatic_head_tails(conn, lab, dive_id, [AutomaticHeadTailRow(
            capture_id=c, status="predicted", predictor_version=1, laser_x=2049.0,
            laser_y=1601.0, head_x=1905.0, head_y=1600.0, tail_x=2210.0,
            tail_y=1600.0, mask_bbox=[1, 2, 3, 4], sam_score=0.9, checkpoint="s",
        )])  # fmt: skip


async def test_frames_are_the_human_labelled_frames_of_the_paper_dives(
    owner_engine, app_engine, references
):
    await references("Grouper", 0.36)
    lab = await tenant(owner_engine)
    pool = await _paper_dive(owner_engine, lab, 58)
    reef = await _paper_dive(owner_engine, lab, 347)
    model = await _labelled(owner_engine, lab, pool, "Fish Model, Grouper")
    fish = await _labelled(owner_engine, lab, reef, None, green=True)
    await _labelled(owner_engine, lab, reef, "Slate, Laser on slate")
    await _auto(app_engine, lab, pool, model)

    async with tenant_transaction(app_engine, lab) as conn:
        frames = await validation_frames(conn, lab, pool_dives=[58], reef_dives=[347])

    by_set = {f.set: f for f in frames}
    assert len(frames) == 2
    p, r = by_set["pool"], by_set["reef"]
    assert (p.model, p.true_length_m) == ("Grouper", 0.36)
    assert p.human_dot == (2048.0, 1600.0) and p.auto_dot == (2049.0, 1601.0)
    assert p.auto_head_tail == (1905.0, 1600.0, 2210.0, 1600.0)
    assert p.stored_calibration[0] == [0.1, 0.0, 0.0]
    assert p.label_free_calibration is None
    assert r.green and r.auto_head_tail is None and r.model is None
    assert fish != model  # the slate frame is not among them


async def test_outputs_from_a_file_replace_the_databases(
    owner_engine, app_engine, references
):
    await references("Ruler", 0.3429)
    lab = await tenant(owner_engine)
    pool = await _paper_dive(owner_engine, lab, 58)
    c = await _labelled(owner_engine, lab, pool, "Calibration Targets, Ruler")
    async with owner_engine.connect() as conn:
        from sqlalchemy import text

        number = (
            await conn.execute(
                text("SELECT number FROM captures WHERE id = :c"), {"c": c}
            )
        ).scalar_one()
    outputs = {number: {"auto_dot": [1.0, 2.0], "auto_head_tail": None,
                        "auto_head_tail_humandot": [5.0, 6.0, 7.0, 8.0]}}  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        (f,) = await validation_frames(conn, lab, pool_dives=[58], reef_dives=[],
                                       outputs=outputs)  # fmt: skip

    assert (f.model, f.true_length_m) == ("Ruler", 0.3429)
    assert f.auto_dot == (1.0, 2.0) and f.auto_head_tail is None
    assert f.auto_head_tail_humandot == (5.0, 6.0, 7.0, 8.0)


async def test_a_frame_the_file_does_not_name_was_not_run(
    owner_engine, app_engine, references
):
    """The paper ran a sample of some dives (200 frames each of 362 and 366):
    a frame it never ran is no evidence either way, so it is left out."""
    await references("Ruler", 0.3429)
    lab = await tenant(owner_engine)
    pool = await _paper_dive(owner_engine, lab, 58)
    await _labelled(owner_engine, lab, pool, "Calibration Targets, Ruler")

    async with tenant_transaction(app_engine, lab) as conn:
        frames = await validation_frames(conn, lab, pool_dives=[58], reef_dives=[],
                                         outputs={})  # fmt: skip

    assert frames == []


async def test_the_cli_prints_the_papers_tables(owner_engine, app_engine, app_url,
                                                monkeypatch, capsys, references):  # fmt: skip
    await references("Grouper", 0.36)
    lab = await tenant(owner_engine)
    pool = await _paper_dive(owner_engine, lab, 58)
    c = await _labelled(owner_engine, lab, pool, "Fish Model, Grouper")
    await _auto(app_engine, lab, pool, c)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(["validate-automatic", "--tenant", str(lab)]) == 0

    out = capsys.readouterr().out
    assert "POOL (Table 4)" in out and "REEF" in out
    assert "  E    auto  auto" in out
    # Phase C: Table 3 for the paper's sessions; the reef is the paper's own
    # frames unless told otherwise.
    assert "TABLE 3" in out and "rejected by labellers" in out
    assert "paper's 791 reef frames" in out


async def test_the_cli_reads_label_free_calibrations_from_a_file(
    owner_engine, app_engine, app_url, monkeypatch, capsys, tmp_path, references
):
    await references("Grouper", 0.36)
    lab = await tenant(owner_engine)
    pool = await _paper_dive(owner_engine, lab, 58)
    c = await _labelled(owner_engine, lab, pool, "Fish Model, Grouper")
    await _auto(app_engine, lab, pool, c)
    cal = tmp_path / "label_free.json"
    cal.write_text(json.dumps({"58": {"laser_position": [0.1, 0, 0],
                                      "laser_axis": [0, 0, 1]}}))  # fmt: skip
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    assert await main(["validate-automatic", "--tenant", str(lab),
                       "--label-free", str(cal)]) == 0  # fmt: skip

    out = capsys.readouterr().out
    f_row = next(line for line in out.splitlines() if line.startswith("  F "))
    assert " 1 " in f_row  # one frame measured with the file's calibration


@pytest.mark.parametrize("tenant_arg", ["no-such-tenant"])
async def test_an_unknown_tenant_is_an_error(app_url, monkeypatch, tenant_arg):
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    assert await main(["validate-automatic", "--tenant", tenant_arg]) == 1


# -- Phase C (2026-10-18): the paper's own reef frames, rejected ones, Table 3 ------


async def test_the_papers_listed_reef_frames_include_the_rejected_ones(
    owner_engine, app_engine
):
    """Given the paper's frame list (v1 image ids), a reef dive's frames are
    exactly those, and a listed frame whose head/tail label has no points --
    a labeller's "not measurable" -- is in, as rejected."""
    lab = await tenant(owner_engine)
    reef = await _paper_dive(owner_engine, lab, 347)
    measured = await _labelled(owner_engine, lab, reef, None)
    rejected = await capture(owner_engine, lab, reef)
    await laser_label(owner_engine, lab, rejected, x=2048.0, y=1600.0)
    await exec_(owner_engine, "INSERT INTO head_tail_labels (tenant_id, capture_id, "
                "source, ls_project_id, completed, superseded) VALUES "
                "(:t, :c, 'human', 1, true, false)", t=lab, c=rejected)  # fmt: skip
    unlisted = await _labelled(owner_engine, lab, reef, None)
    for c, v1 in ((measured, 9001), (rejected, 9002), (unlisted, 9003)):
        await exec_(owner_engine, "UPDATE captures SET v1_id = :v WHERE id = :c",
                    v=v1, c=c)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        frames = await validation_frames(
            conn, lab, pool_dives=[], reef_dives=[347],
            reef_frames={9001: (347, True), 9002: (347, False)},
        )  # fmt: skip

    by_rejected = {f.rejected: f for f in frames}
    assert len(frames) == 2
    assert by_rejected[True].human_head_tail is None
    assert by_rejected[False].human_head_tail == (1900.0, 1600.0, 2200.0, 1600.0)


async def test_a_sessions_calibrations_are_its_stored_axis_and_its_own_fit(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    session = await _paper_dive(owner_engine, lab, 62)
    async with tenant_transaction(app_engine, lab) as conn:
        await persist_automatic_calibration(conn, lab, session, AutomaticCalibrationRow(
            outcome="accepted", algorithm_version="1", laser_position=[0.03, -0.1, 0.0],
            laser_axis=[0.01, 0.03, 0.9995], vanishing_px=-2000.0,
            line_direction=[-0.29, -0.957], line_offset_px=1500.0, o_mag_m=0.104,
            frames_used=12, pair_count=40, size_ratio=2.1, se_px=0.9,
            pair_residual_sd=0.01, capture_ids=[], core_version="4.1.0",
        ))  # fmt: skip
        (s,) = await session_calibrations(conn, lab, [62])

    assert s.dive_number == 62
    assert s.stored_axis is not None
    assert (s.vanishing_px, list(s.line_direction)) == (-2000.0, [-0.29, -0.957])
