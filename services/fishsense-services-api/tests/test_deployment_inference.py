"""Deployments inferred for legacy missions from their folders and dates (the
owner, 2026-10-20).

A plan is read-only and carries a fingerprint; applying needs the fingerprint
of the plan that was reviewed, writes under guards on the exact old values,
and writes nothing when anything moved since the review.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.deployment_inference import (
    PlanChanged,
    apply_inference,
    folder_date,
    place_of,
    plan_inference,
)

SMILE = "2025-02-10 REEF Data Dump SMILE 6"
REEF = "2024.06.20.REEF"
DUMP = "2023-09-07 REEF Data Dump"


async def _tenant(owner_engine, slug: str) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": f"{slug}-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()


async def _device(owner_engine, tenant_id, serial: str) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO devices (tenant_id, kind, serial) "
                    "VALUES (:t, 'lite', :s) RETURNING id"
                ),
                {"t": tenant_id, "s": serial},
            )
        ).scalar_one()


async def _mission(
    owner_engine, tenant_id, path, *, device, at, frames=3, deployment=None
) -> int:
    """A mission with ``frames`` captures from ``at`` on, a minute apart."""
    async with owner_engine.begin() as conn:
        mission_id, number = (
            await conn.execute(
                text("""
                    INSERT INTO missions (tenant_id, source_path, name, started_at,
                                          device_id, deployment_id)
                    VALUES (:t, :p, :n, :at, :dev, :dep) RETURNING id, number
                    """),
                {"t": tenant_id, "p": path, "n": path.rsplit("/", 1)[-1],
                 "at": at, "dev": device, "dep": deployment},
            )
        ).one()  # fmt: skip
        for i in range(frames):
            await conn.execute(
                text("""
                    INSERT INTO captures (tenant_id, mission_id, device_id,
                                          source_path, captured_at, checksum)
                    VALUES (:t, :m, :dev, :p, :at, :sum)
                    """),
                {"t": tenant_id, "m": mission_id, "dev": device,
                 "p": f"{path}/P{i:07d}.ORF", "at": at + timedelta(minutes=i),
                 "sum": uuid.uuid4().hex},
            )  # fmt: skip
    return number


async def _deployment(owner_engine, tenant_id, name, started_on) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO deployments (tenant_id, name, source_path, "
                    "started_on) VALUES (:t, :n, :n, :s) RETURNING id"
                ),
                {"t": tenant_id, "n": name, "s": started_on},
            )
        ).scalar_one()


async def _deployments_of(owner_engine, tenant_id) -> dict[int, str | None]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("""
                SELECT m.number, d.name FROM missions m
                LEFT JOIN deployments d ON d.id = m.deployment_id
                WHERE m.tenant_id = :t
                """),
            {"t": tenant_id},
        )
        return {r.number: r.name for r in rows}


async def _plan(app_engine, tenant_id, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await plan_inference(conn, tenant_id, **kwargs)


async def _apply(app_engine, tenant_id, fingerprint, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await apply_inference(conn, tenant_id, expect=fingerprint, **kwargs)


def _trip_of(plan) -> dict[int, str | None]:
    return {m: t.name for t in plan.trips for m in t.missions}


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# -- the folder evidence, as pure functions -----------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        (f"{SMILE}/090123_SMILE Dives_Alligator/090123_FSL04_SH/x", date(2023, 9, 1)),
        # The nearest folder with a date decides: a month folder is not a day.
        (f"{REEF}/06_2024_Alligator/062024_Alligator_FSL01", date(2024, 6, 20)),
        (f"{SMILE}/08_20_24_Alligator_FSL01/082024_Alligator1_FSL01",
         date(2024, 8, 20)),  # fmt: skip
        (f"{SMILE}/8.17.2023_Pompano_FSL-01", date(2023, 8, 17)),
        (f"{SMILE}/SMILE Caymans 020324_FSL06", date(2024, 2, 3)),
        # A dump's own date is when it was copied, not when anyone dived.
        (f"{DUMP}/080123_FSL-01 Photos", date(2023, 8, 1)),
        (f"{SMILE}/E4E Research Workspace Folders_Measured/H Slate Dive 1", None),
        (f"{REEF}/09_2023_Islamorada/092223_FSL03", date(2023, 9, 22)),
        # 1/17 or 11/7? Five digits do not say.
        (f"{SMILE}/11723_FSL-05_Photos", None),
        # ... nor does the folder above it.
        (f"{SMILE}/111323_Alligator/11723_FSL-05_Photos", None),
        # An e4edm dataset's name is its date.
        ("/fishsense_data/2025.01.17.FishSense.San Diego/ED-00/FSL-10D/Box",
         date(2025, 1, 17)),  # fmt: skip
    ],
)
def test_a_missions_folder_date(path, expected):
    assert folder_date(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "2024.13.40.FishSense.Keys/ED-00/FSL-01D/Reef",  # no such day
        "2024.02.30.FishSense.Keys/ED-00/FSL-01D/Reef",
        "2031.01.01.FishSense.Keys/ED-00/FSL-01D/Reef",  # after it was ingested
        "2014.01.01.FishSense.Keys/ED-00/FSL-01D/Reef",  # before FishSense
    ],
)
def test_a_dataset_name_that_is_no_date_is_unreadable_not_an_error(path):
    assert folder_date(path, latest=date(2026, 10, 20)) is None


def test_a_folder_date_after_the_mission_was_ingested_is_not_read():
    """The bound is the mission's own ingest, not the day the plan is made, so
    a plan reviewed today is the plan applied tomorrow."""
    path = f"{SMILE}/120624_ElbowReef_FSL01"

    assert folder_date(path, latest=date(2024, 12, 6)) == date(2024, 12, 6)
    assert folder_date(path, latest=date(2024, 12, 5)) is None


def test_a_date_code_that_is_no_date_is_not_read_from_the_folder_above():
    """082329 is no date (2029): the captures say, not the parent's 082923."""
    path = f"{DUMP}/082923_Pool Calibration/082329_Slate_FSL05"

    assert folder_date(path, latest=date(2026, 10, 20)) is None


@pytest.mark.parametrize(
    "path, expected",
    [
        (f"{SMILE}/090123_SMILE Dives_Alligator/090123_FSL04_SH/x_DavisReef_FSL04",
         "Islamorada"),  # fmt: skip
        (f"{SMILE}/E4E Research Workspace Folders_Measured/Hogfish01_MolHITW_0926",
         "Key Largo"),  # fmt: skip
        # The nearest folder naming a place decides.
        (f"{SMILE}/082323_Pool Validation/062524_Alligator_FSL04/062524_Alligator0",
         "Islamorada"),  # fmt: skip
        (f"{SMILE}/CubaTrip2024_FSL03/062324_LosMogotes_FSL03", "Cuba"),
        (f"{SMILE}/07_2024_FSL01/070924_Dive1_FSL01", None),
    ],
)
def test_a_missions_place(path, expected):
    assert place_of(path) == expected


# -- trips ---------------------------------------------------------------------


async def test_one_place_within_the_gap_is_one_trip_and_beyond_it_another(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    fsl = await _device(owner_engine, lab, "FSL-01")
    a = await _mission(owner_engine, lab, f"{SMILE}/101524_Alligator/101524_A_FSL01",
                       device=fsl, at=_utc(2024, 10, 15, 14))  # fmt: skip
    b = await _mission(owner_engine, lab, f"{SMILE}/101624_Alligator/101624_A_FSL01",
                       device=fsl, at=_utc(2024, 10, 16, 14))  # fmt: skip
    c = await _mission(owner_engine, lab, f"{SMILE}/102724_Islamorada/102724_Chutes",
                       device=fsl, at=_utc(2024, 10, 27, 14))  # fmt: skip

    trips = _trip_of(await _plan(app_engine, lab))

    assert trips[a] == trips[b] == "2024.10.15.FishSense.Islamorada"
    assert trips[c] == "2024.10.27.FishSense.Islamorada"


async def test_the_folder_date_beats_a_camera_clock_a_year_out(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    six = await _device(owner_engine, lab, "FSL-06")
    one = await _device(owner_engine, lab, "FSL-01")
    off = await _mission(owner_engine, lab,
                         f"{SMILE}/102724_Islamorada_FSL06/102724_Chutes_FSL06",
                         device=six, at=_utc(2023, 10, 27, 15))  # fmt: skip
    on = await _mission(owner_engine, lab,
                        f"{SMILE}/102724_Islamorada_FSL01/102724_Chutes_FSL01",
                        device=one, at=_utc(2024, 10, 27, 15))  # fmt: skip

    plan = await _plan(app_engine, lab)

    assert _trip_of(plan)[off] == _trip_of(plan)[on]
    (trip,) = plan.trips
    assert (trip.started_on, trip.ended_on) == (date(2024, 10, 27), date(2024, 10, 27))
    # The disagreement is shown to the review.
    assert any(f.mission == off and "2023-10-27" in f.note for f in trip.flags)


async def test_without_a_readable_folder_date_the_last_capture_decides(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    five = await _device(owner_engine, lab, "FSL-05")
    typo = await _mission(owner_engine, lab,
                          f"{DUMP}/082923_Pool Calibration/082329_Slate_FSL05",
                          device=five, at=_utc(2023, 8, 23, 10))  # fmt: skip

    plan = await _plan(app_engine, lab)

    assert _trip_of(plan)[typo] == "2023.08.23.FishSense.Pool"


async def test_copies_of_one_session_in_several_dumps_share_one_trip_and_evidence(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    three = await _device(owner_engine, lab, "FSL-03")
    at = _utc(2023, 9, 22, 13)
    # Only one copy's folders say where.
    copies = [
        await _mission(owner_engine, lab, path, device=three, at=at)
        for path in (
            f"{REEF}/09_2023_Islamorada/092223_FSL03",
            f"{SMILE}/092223_FSL-03 Photos",
            "drive-download-20240307T1050Z/092223_FSL-03 Photos",
        )
    ]

    plan = await _plan(app_engine, lab)

    assert {_trip_of(plan)[m] for m in copies} == {"2023.09.22.FishSense.Islamorada"}
    (trip,) = plan.trips
    assert trip.sessions == 1
    # From each other, not from where their camera was.
    assert not any("its camera" in f.note for f in trip.flags)


async def test_a_copy_without_a_folder_date_takes_its_copys(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    six = await _device(owner_engine, lab, "FSL-06")
    clock = _utc(2023, 10, 27, 15)  # a year out
    dated = await _mission(owner_engine, lab,
                           f"{SMILE}/102724_Islamorada_FSL06/102724_Victory_FSL06",
                           device=six, at=clock)  # fmt: skip
    undated = await _mission(owner_engine, lab, "drive-download-x/Victory_FSL06",
                             device=six, at=clock)  # fmt: skip

    trips = _trip_of(await _plan(app_engine, lab))

    assert trips[undated] == trips[dated] == "2024.10.27.FishSense.Islamorada"


async def test_a_session_with_no_place_joins_its_cameras_trip_within_the_gap(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    two = await _device(owner_engine, lab, "FSL-02")
    dive = await _mission(owner_engine, lab,
                          f"{SMILE}/E4E Research/Hogfish01_MolHITW_0926_080323",
                          device=one, at=_utc(2023, 8, 3, 9))  # fmt: skip
    slate = await _mission(owner_engine, lab, f"{SMILE}/E4E Research/H Slate Dive 1",
                           device=one, at=_utc(2023, 8, 3, 12))  # fmt: skip
    # Another camera: not evidence of where this one was.
    other = await _mission(owner_engine, lab, f"{SMILE}/080323_Photos_FSL02",
                           device=two, at=_utc(2023, 8, 3, 9))  # fmt: skip

    trips = _trip_of(await _plan(app_engine, lab))

    assert trips[slate] == trips[dive] == "2023.08.03.FishSense.Key Largo"
    assert trips[other] == "2023.08.03.FishSense"


async def test_an_operator_puts_a_mission_with_another(owner_engine, app_engine):
    """082024_Dive1_FSL05_fix site was dived beside FSL-01's 070924_Dive1."""
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    five = await _device(owner_engine, lab, "FSL-05")
    july = await _mission(owner_engine, lab, f"{SMILE}/07_2024_FSL01/070924_Dive1",
                          device=one, at=_utc(2024, 7, 9, 21))  # fmt: skip
    misfiled = await _mission(owner_engine, lab,
                              f"{SMILE}/08_20_24_Alligator_FSL05/082024_Dive1_fix site",
                              device=five, at=_utc(2024, 7, 9, 21))  # fmt: skip

    before = _trip_of(await _plan(app_engine, lab))
    after = _trip_of(await _plan(app_engine, lab, together={misfiled: july}))

    assert before[misfiled] == "2024.08.20.FishSense.Islamorada"
    assert after[misfiled] == after[july] == "2024.07.09.FishSense"
    with pytest.raises(ValueError, match="no such mission"):
        await _plan(app_engine, lab, together={misfiled: 999999})
    # The review sees why it moved, not the folder date it no longer uses.
    (trip,) = [
        t for t in (await _plan(app_engine, lab, together={misfiled: july})).trips
        if misfiled in t.missions
    ]  # fmt: skip
    assert [f.note for f in trip.flags if f.mission == misfiled] == [
        f"put with mission {july} by the operator"
    ]


async def test_a_mission_put_with_one_placed_by_its_camera_follows_it(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    five = await _device(owner_engine, lab, "FSL-05")
    await _mission(owner_engine, lab, f"{SMILE}/070924_Molasses_FSL01", device=one,
                   at=_utc(2024, 7, 9, 15))  # fmt: skip
    unplaced = await _mission(owner_engine, lab, f"{SMILE}/07_2024_FSL01/070924_Dive1",
                              device=one, at=_utc(2024, 7, 9, 21))  # fmt: skip
    moved = await _mission(owner_engine, lab,
                           f"{SMILE}/08_20_24_Alligator_FSL05/082024_Dive1_fix site",
                           device=five, at=_utc(2024, 7, 9, 21))  # fmt: skip

    # Another FSL-05 session that day, with no place: the moved mission's own
    # folders (Alligator) were overruled, so they place nothing.
    beside = await _mission(owner_engine, lab, f"{SMILE}/070924_Photos_FSL05",
                            device=five, at=_utc(2024, 7, 9, 15))  # fmt: skip

    trips = _trip_of(await _plan(app_engine, lab, together={moved: unplaced}))

    assert trips[moved] == trips[unplaced] == "2024.07.09.FishSense.Key Largo"
    assert trips[beside] == "2024.07.09.FishSense"


async def test_chained_choices_are_refused(owner_engine, app_engine):
    """5=6 with 6=7 would depend on which is applied first."""
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    a, b, c = [
        await _mission(owner_engine, lab, f"{SMILE}/07092{i}_Dive_FSL01", device=one,
                       at=_utc(2024, 7, 9 + i, 15))  # fmt: skip
        for i in range(3)
    ]

    with pytest.raises(ValueError, match="chain"):
        await _plan(app_engine, lab, together={a: b, b: c})


async def test_a_negative_gap_is_refused(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")

    with pytest.raises(ValueError, match="gap"):
        await _plan(app_engine, lab, gap_days=-1)


async def test_a_trip_holding_a_grouped_mission_joins_its_deployment(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    trip = await _deployment(owner_engine, lab, "2023.08.03.FishSense.FSL-01D",
                             date(2023, 8, 3))  # fmt: skip
    at = _utc(2023, 8, 3, 9)
    grouped = await _mission(owner_engine, lab,
                             "2023.08.03.FishSense.FSL-01D/Hogfish01_MolHITW_0926_080323",
                             device=one, at=at, deployment=trip)  # fmt: skip
    copy = await _mission(owner_engine, lab,
                          f"{SMILE}/E4E Research/Hogfish01_MolHITW_0926_080323",
                          device=one, at=at)  # fmt: skip
    earlier = await _mission(owner_engine, lab, f"{DUMP}/080123_FSL-01 Photos",
                             device=one, at=_utc(2023, 8, 1, 11))  # fmt: skip

    plan = await _plan(app_engine, lab)

    (planned,) = plan.trips
    assert planned.name == "2023.08.03.FishSense.FSL-01D" and planned.exists
    assert planned.deployment_id == trip
    assert set(planned.missions) == {grouped, copy, earlier}
    assert set(planned.to_write) == {copy, earlier}

    await _apply(app_engine, lab, plan.fingerprint)

    assert set((await _deployments_of(owner_engine, lab)).values()) == {
        "2023.08.03.FishSense.FSL-01D"
    }


async def test_an_unplaced_legacy_mission_does_not_join_an_unplaced_deployment(
    owner_engine, app_engine
):
    """An e4edm dataset at a place `PLACES` does not know is anchored by its
    deployment, not pooled with every unplaced mission nearby."""
    lab = await _tenant(owner_engine, "lab")
    nine = await _device(owner_engine, lab, "FSL-09")
    two = await _device(owner_engine, lab, "FSL-02")
    bonaire = await _deployment(owner_engine, lab, "2024.06.20.FishSense.Bonaire",
                                date(2024, 6, 20))  # fmt: skip
    await _mission(owner_engine, lab, "2024.06.20.FishSense.Bonaire/ED-00/FSL-09D/Reef",
                   device=nine, at=_utc(2024, 6, 20, 15), deployment=bonaire)  # fmt: skip
    legacy = await _mission(owner_engine, lab, f"{SMILE}/062124_Photos_FSL02",
                            device=two, at=_utc(2024, 6, 21, 15))  # fmt: skip

    trips = _trip_of(await _plan(app_engine, lab))

    assert trips[legacy] == "2024.06.21.FishSense"


async def test_a_trip_spanning_two_deployments_is_left_alone_and_said(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    a = await _deployment(owner_engine, lab, "2024.01.01.X.Islamorada", None)
    b = await _deployment(owner_engine, lab, "2024.01.02.X.Islamorada", None)
    await _mission(owner_engine, lab, f"{SMILE}/010124_Alligator_FSL01", device=one,
                   at=_utc(2024, 1, 1, 15), deployment=a)  # fmt: skip
    await _mission(owner_engine, lab, f"{SMILE}/010224_Alligator_FSL01", device=one,
                   at=_utc(2024, 1, 2, 15), deployment=b)  # fmt: skip
    loose = await _mission(owner_engine, lab, f"{REEF}/010224_Alligator_FSL01b",
                           device=one, at=_utc(2024, 1, 2, 18))  # fmt: skip

    plan = await _plan(app_engine, lab)

    (trip,) = plan.trips
    assert trip.conflict and trip.to_write == ()
    assert loose in trip.missions


async def test_a_new_trip_whose_name_is_taken_is_left_alone_and_said(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    await _deployment(owner_engine, lab, "2024.12.06.FishSense.Key Largo", None)
    await _mission(owner_engine, lab, f"{SMILE}/120624_ElbowReef_FSL01/x",
                   device=one, at=_utc(2024, 12, 6, 15))  # fmt: skip

    (trip,) = (await _plan(app_engine, lab)).trips

    assert trip.conflict and trip.to_write == ()
    assert any("already names" in f.note for f in trip.flags)


# -- applying --------------------------------------------------------------------


async def test_applying_the_reviewed_plan_writes_it(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    a = await _mission(owner_engine, lab, f"{SMILE}/120624_ElbowReef_FSL01/x",
                       device=one, at=_utc(2024, 12, 6, 15))  # fmt: skip
    b = await _mission(owner_engine, lab, f"{SMILE}/120624_ElbowReef_FSL01/y",
                       device=one, at=_utc(2024, 12, 6, 18))  # fmt: skip
    plan = await _plan(app_engine, lab)

    applied = await _apply(app_engine, lab, plan.fingerprint)

    assert (applied.missions, applied.deployments) == (2, 1)
    assert await _deployments_of(owner_engine, lab) == {
        a: "2024.12.06.FishSense.Key Largo",
        b: "2024.12.06.FishSense.Key Largo",
    }
    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT started_on, ended_on, source_path, notes "
                     "FROM deployments WHERE tenant_id = :t"),
                {"t": lab},
            )
        ).one()  # fmt: skip
    assert (row.started_on, row.ended_on, row.source_path) == (
        date(2024, 12, 6),
        date(2024, 12, 6),
        None,
    )
    assert "inferred" in row.notes
    # Nothing is left to infer.
    assert all(t.to_write == () for t in (await _plan(app_engine, lab)).trips)


async def test_a_stale_fingerprint_writes_nothing(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    await _mission(owner_engine, lab, f"{SMILE}/120624_ElbowReef_FSL01/x",
                   device=one, at=_utc(2024, 12, 6, 15))  # fmt: skip
    plan = await _plan(app_engine, lab)
    await _mission(owner_engine, lab, f"{SMILE}/120624_ElbowReef_FSL01/y",
                   device=one, at=_utc(2024, 12, 6, 18))  # fmt: skip

    with pytest.raises(PlanChanged):
        await _apply(app_engine, lab, plan.fingerprint)

    assert set((await _deployments_of(owner_engine, lab)).values()) == {None}


async def test_the_operators_choices_are_part_of_the_fingerprint(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    one = await _device(owner_engine, lab, "FSL-01")
    a = await _mission(owner_engine, lab, f"{SMILE}/070924_Dive1_FSL01",
                       device=one, at=_utc(2024, 7, 9, 21))  # fmt: skip
    b = await _mission(owner_engine, lab, f"{SMILE}/082024_Alligator_FSL01",
                       device=one, at=_utc(2024, 8, 20, 15))  # fmt: skip

    plain = await _plan(app_engine, lab)
    chosen = await _plan(app_engine, lab, together={b: a})
    # A choice that changes no trip is still part of what was reviewed.
    same = await _plan(app_engine, lab, together={a: a})

    assert plain.fingerprint != chosen.fingerprint
    assert _trip_of(same) == _trip_of(plain) and same.fingerprint != plain.fingerprint
    with pytest.raises(PlanChanged):
        await _apply(app_engine, lab, plain.fingerprint, together={b: a})
