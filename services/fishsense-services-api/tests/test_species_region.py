"""Where a mission was, for the species BioCLIP may name (0085; walking up
the tree, the owner, 2026-11-12).

Of the 481 REEF missions, 459 name their place in their own folder. The
others are folders like ``092223_FSL-03 Photos``: the same files sit under
``09_2023_Islamorada`` in another dump, or the mission's deployment is named
for its place, or its other missions all dove there. What is left is REEF's
with no place at all, the Unknown REEF Region, offered the REEF targets alone
(the owner, the same day). Pinned here:

* a mission's own folder names its region, as before;
* else another copy of its files, in a folder naming a place, does;
* else its deployment's name, or its deployment's other missions when they
  all agree;
* copies or missions that disagree say nothing;
* else a reef mission is in the Unknown REEF Region, with no regional fish
  (a species row records the region `mission_species_region` gives); any
  other tenant's is in none.
"""

from __future__ import annotations

import uuid

from depth_measure_seed import T0, capture, exec_, tenant
from fishsense_services_api.regional_species import (
    UNKNOWN_REEF_REGION,
    automatic_species_list,
    mission_species_region,
)
from test_automatic_results_store import _backlog_mission, _in

KEYS = "Florida Keys, USA"


async def _at(owner_engine, tenant_id, source_path, *, deployment=None):
    mission_id = await _backlog_mission(owner_engine, tenant_id)
    await exec_(owner_engine,
                "UPDATE missions SET source_path = :p, deployment_id = :dep WHERE id = :d",
                p=source_path, dep=deployment, d=mission_id)  # fmt: skip
    return mission_id


async def _deployment(owner_engine, tenant_id, name) -> uuid.UUID:
    deployment_id = uuid.uuid4()
    await exec_(owner_engine,
                "INSERT INTO deployments (id, tenant_id, name) VALUES (:id, :t, :n)",
                id=deployment_id, t=tenant_id, n=name)  # fmt: skip
    return deployment_id


async def _same_files(owner_engine, a, b):
    """Mission ``b`` holds a copy of one of ``a``'s files."""
    await exec_(owner_engine, """
        UPDATE captures SET checksum = (SELECT checksum FROM captures WHERE mission_id = :a LIMIT 1)
        WHERE mission_id = :b
        """, a=a, b=b)  # fmt: skip


async def _region(app_engine, tenant_id, mission_id):
    return await _in(app_engine, tenant_id, mission_species_region, mission_id)


async def test_its_own_folder_names_it(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _at(owner_engine, lab, "2024.06.20.REEF/09_2023_Islamorada/092223_FSL03")
    assert await _region(app_engine, lab, m) == KEYS


async def test_else_a_copy_of_its_files_does(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    bare = await _at(owner_engine, lab, "drive-download/092223_FSL-03 Photos")
    placed = await _at(
        owner_engine, lab, "2024.06.20.REEF/09_2023_Islamorada/092223_FSL03"
    )
    await capture(owner_engine, lab, bare)
    await capture(owner_engine, lab, placed, canonical=False)
    await _same_files(owner_engine, bare, placed)

    assert await _region(app_engine, lab, bare) == KEYS


async def test_copies_that_disagree_say_nothing(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    bare = await _at(owner_engine, lab, "dump/080123_FSL-01 Photos")
    keys = await _at(owner_engine, lab, "dump/09_2023_Islamorada/a")
    cuba = await _at(owner_engine, lab, "dump/CubaTrip2024_FSL06/b")
    await capture(owner_engine, lab, bare)
    for other in (keys, cuba):
        await capture(owner_engine, lab, other, canonical=False)
        await _same_files(owner_engine, bare, other)

    assert await _region(app_engine, lab, bare) is None


async def test_else_its_deployments_name_does(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    trip = await _deployment(owner_engine, lab, "2023.09.22.FishSense.Islamorada")
    m = await _at(owner_engine, lab, "dump/092223_FSL-03 Photos", deployment=trip)
    assert await _region(app_engine, lab, m) == KEYS


async def test_else_its_deployments_other_missions_when_they_agree(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    trip = await _deployment(owner_engine, lab, "2023.08.03.FishSense.FSL-01D")
    m = await _at(owner_engine, lab, "dump/080123_FSL-01 Photos", deployment=trip)
    await _at(
        owner_engine,
        lab,
        "dump/080323_FSL-01 Photos/080323_1015_MolPeLe",
        deployment=trip,
    )
    await _at(
        owner_engine,
        lab,
        "dump/080323_FSL-01 Photos/080323_1419_ConchReef",
        deployment=trip,
    )
    await _at(owner_engine, lab, "dump/H slate dive 1", deployment=trip)

    assert await _region(app_engine, lab, m) == KEYS


async def test_a_deployments_missions_that_disagree_say_nothing(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    trip = await _deployment(owner_engine, lab, "2024.07.09.FishSense")
    m = await _at(
        owner_engine, lab, "dump/07_2024_FSL01/070924_Dive1_FSL01", deployment=trip
    )
    await _at(owner_engine, lab, "dump/09_2023_Islamorada/a", deployment=trip)
    await _at(owner_engine, lab, "dump/CubaTrip2024_FSL06/b", deployment=trip)

    assert await _region(app_engine, lab, m) is None


async def test_a_reef_mission_with_no_place_is_in_the_unknown_reef_region(
    owner_engine, app_engine
):
    reef = await tenant(owner_engine, "reef")
    m = await _at(owner_engine, reef, "2025-02-10 REEF Data Dump SMILE 6/073124")

    offered = await _in(app_engine, reef, automatic_species_list, m)

    assert UNKNOWN_REEF_REGION == "Unknown REEF Region"
    assert (offered.region, offered.species) == (UNKNOWN_REEF_REGION, [])


async def test_another_tenants_mission_with_no_place_is_in_none(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _at(owner_engine, lab, "2025-02-10 REEF Data Dump SMILE 6/073124")

    offered = await _in(app_engine, lab, automatic_species_list, m)

    assert (offered.region, offered.species) == (None, [])


async def test_a_mission_whose_folder_and_photos_disagree_on_the_day_says_nothing(
    owner_engine, app_engine
):
    """Production, 2026-11-12: mission 398's folder says Alligator on
    2024-08-20, its photos 2024-07-09, so the deployments grouped it with
    the July dives (399-401) and placed them in the Keys. One of the two is
    wrong; either way it is no evidence of where July's dives were."""
    lab = await tenant(owner_engine)
    trip = await _deployment(owner_engine, lab, "2024.07.09.FishSense")
    m = await _at(
        owner_engine, lab, "dump/07_2024_FSL01/070924_Dive1_FSL01", deployment=trip
    )
    misfiled = await _at(owner_engine, lab,
                         "dump/08_20_24_Alligator_FSL05/082024_Dive1_FSL05_fix site",
                         deployment=trip)  # fmt: skip
    await capture(owner_engine, lab, misfiled)  # dated T0, not 2024-08-20

    assert await _region(app_engine, lab, m) is None


async def test_a_mission_whose_folder_and_photos_agree_counts(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    trip = await _deployment(owner_engine, lab, "2024.07.09.FishSense")
    m = await _at(
        owner_engine, lab, "dump/07_2024_FSL01/070924_Dive1_FSL01", deployment=trip
    )
    day = T0.strftime("%m%d%y")
    placed = await _at(
        owner_engine, lab, f"dump/{day}_Alligator_FSL05", deployment=trip
    )
    await capture(owner_engine, lab, placed)

    assert await _region(app_engine, lab, m) == KEYS
