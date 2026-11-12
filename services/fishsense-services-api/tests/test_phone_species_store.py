"""Species for the fish on phone captures (0090; the owner, 2026-11-11: "let's
add the missing species and run it").

BioCLIP names the fish in each phone capture the server measured, cropped to
the server measurement's mask, choosing among the fish of a region the run
is told (a phone capture has no folder to find one from, and CCFRP's have no
GPS: the owner, "all of the ccfrp photos were taken in san diego"). A region
offers its 0085 list and the species added for it here: the fish CCFRP
measured that the OBIS list lacks. The result is unvalidated, like the
length it sits beside.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_phone_measurement_store import _measured, _phone

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.observation_store import observation_page
from fishsense_services_api.phone_measurement_store import PhoneMeasurementCatalog
from fishsense_services_api.phone_species_store import (
    PHONE_SPECIES_LIST_VERSION,
    PhoneSpeciesCatalog,
    PhoneSpeciesResult,
    phone_species_list,
)

ORCHESTRATOR = "service:orchestrator"
SOCAL = "Southern California, USA"
VERSION = 1


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ORCHESTRATOR: {"lab": "member", "ccfrp": "member"}})


@pytest.fixture
def catalog(app_engine):
    return PhoneSpeciesCatalog(app_engine, sub=ORCHESTRATOR)


async def _measured_phone(app_engine, tenant_id, *, with_capture=False):
    capture_id, capture = await _phone(app_engine, tenant_id)
    await PhoneMeasurementCatalog(app_engine, sub=ORCHESTRATOR).record(
        tenant_id, [_measured(capture_id)]
    )
    return (capture_id, capture) if with_capture else capture_id


def _predicted(capture, choice="Fish, Calico Rockfish (Sebastes dallii)", **changes):
    values = dict(
        capture_id=capture.capture_id,
        phone_measurement_id=capture.phone_measurement_id,
        status="predicted", predictor_version=VERSION,
        model_id="hf-hub:imageomics/bioclip-2.5-vith14", predicted_choice=choice,
        top1_probability=0.81, margin=0.6,
        top5=[{"choice": choice, "probability": 0.81}],
    )  # fmt: skip
    values.update(changes)
    return PhoneSpeciesResult(**values)


async def _owed(catalog, tenant_id):
    [mission] = await catalog.owed_missions(tenant_id, SOCAL, predictor_version=VERSION)
    return await catalog.owed_captures(
        tenant_id, mission.mission_id, SOCAL, predictor_version=VERSION
    )


async def test_a_region_offers_its_list_and_the_species_added_for_it(app_engine):
    async with app_engine.connect() as conn:
        offered = await phone_species_list(conn, SOCAL)

    names = [scientific for _, scientific in offered]
    assert names[:3] == [
        "Paralabrax clathratus",
        "Oxyjulis californica",
        "Bodianus pulcher",
    ]
    # CCFRP measured these (OCEANS 2025's Fig. 8); OBIS's 45 lack them.
    assert ("Calico Rockfish", "Sebastes dallii") in offered
    assert ("Honeycomb Rockfish", "Sebastes umbrosus") in offered
    assert ("Lingcod", "Ophiodon elongatus") in offered
    assert len(names) == len(set(names))


async def test_a_region_with_no_list_is_refused(app_engine):
    async with app_engine.connect() as conn:
        with pytest.raises(ValueError, match="Atlantis"):
            await phone_species_list(conn, "Atlantis")


async def test_the_work_is_each_measured_phone_fish_with_its_photo_and_mask(
    tenants, catalog, app_engine
):
    capture_id, capture = await _measured_phone(
        app_engine, tenants["lab"], with_capture=True
    )
    no_fish, _ = await _phone(app_engine, tenants["lab"])
    await PhoneMeasurementCatalog(app_engine, sub=ORCHESTRATOR).record(
        tenants["lab"],
        [_measured(no_fish, status="no_fish", length_m=None, snout_x=None,
                   snout_y=None, fork_x=None, fork_y=None, snout_depth_m=None,
                   fork_depth_m=None, mask=None)],
    )  # fmt: skip

    [c] = await _owed(catalog, tenants["lab"])

    assert c.capture_id == capture_id
    assert c.bucket == "fishsense-tenant-lab"
    assert c.rgb_key == {f.role: f.key for f in capture.files}["rgb_jpeg"]
    assert c.mask_key.startswith(f"masks/phone/{capture_id}/")
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        current = (
            await conn.execute(
                text("SELECT id FROM current_phone_measurements WHERE capture_id = :c"),
                {"c": capture_id},
            )
        ).scalar_one()
    assert c.phone_measurement_id == current


async def test_a_fish_with_a_prediction_is_not_work_again(tenants, catalog, app_engine):
    await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])

    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])

    assert (
        await catalog.owed_missions(tenants["lab"], SOCAL, predictor_version=VERSION)
        == []
    )


async def test_another_predictor_or_region_owes_it_again(tenants, catalog, app_engine):
    await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])
    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])

    assert await catalog.owed_missions(tenants["lab"], SOCAL, predictor_version=2)
    assert await catalog.owed_missions(
        tenants["lab"], "Florida Keys, USA", predictor_version=VERSION
    )


async def test_a_new_measurement_owes_it_again(tenants, catalog, app_engine):
    capture_id = await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])
    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])

    await PhoneMeasurementCatalog(app_engine, sub=ORCHESTRATOR).record(
        tenants["lab"], [_measured(capture_id)]
    )

    [again] = await _owed(catalog, tenants["lab"])
    assert again.phone_measurement_id != c.phone_measurement_id


async def test_a_prediction_records_its_region_and_list(
    tenants, catalog, app_engine, owner_engine
):
    await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])

    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT * FROM current_phone_species_predictions"))
        ).one()
    assert (row.species_region, row.species_list_version) == (
        SOCAL,
        PHONE_SPECIES_LIST_VERSION,
    )
    assert (row.capture_id, row.phone_measurement_id) == (
        c.capture_id,
        c.phone_measurement_id,
    )
    assert row.predicted_choice == "Fish, Calico Rockfish (Sebastes dallii)"


async def test_a_prediction_for_an_old_measurement_is_refused(
    tenants, catalog, app_engine
):
    capture_id = await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])
    await PhoneMeasurementCatalog(app_engine, sub=ORCHESTRATOR).record(
        tenants["lab"], [_measured(capture_id)]
    )

    with pytest.raises(ValueError, match="not the capture's current"):
        await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])


async def test_a_choice_the_region_does_not_offer_is_refused(
    tenants, catalog, app_engine
):
    await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])

    with pytest.raises(ValueError, match="Hogfish"):
        await catalog.record(
            tenants["lab"],
            SOCAL,
            [_predicted(c, choice="Fish, Hogfish (Lachnolaimus maximus)")],
        )


async def test_another_tenants_capture_is_refused(tenants, catalog, app_engine):
    await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])

    with pytest.raises(ValueError, match="not the capture's current"):
        await catalog.record(tenants["ccfrp"], SOCAL, [_predicted(c)])


async def test_predictions_are_append_only(tenants, catalog, app_engine):
    await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])
    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            await conn.execute(text("UPDATE phone_species_predictions SET margin = 0"))


async def test_a_phone_fish_on_the_fish_page_has_its_predicted_species(
    tenants, catalog, app_engine
):
    capture_id = await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])
    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        page = await observation_page(conn, tenants["lab"], limit=10)

    (fish,) = page.observations
    assert fish.capture_id == capture_id
    assert (fish.scientific_name, fish.common_name) == (
        "Sebastes dallii",
        "Calico Rockfish",
    )


async def test_a_prediction_for_an_old_measurement_is_not_shown(
    tenants, catalog, app_engine
):
    capture_id = await _measured_phone(app_engine, tenants["lab"])
    [c] = await _owed(catalog, tenants["lab"])
    await catalog.record(tenants["lab"], SOCAL, [_predicted(c)])
    await PhoneMeasurementCatalog(app_engine, sub=ORCHESTRATOR).record(
        tenants["lab"], [_measured(capture_id)]
    )

    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        (fish,) = (await observation_page(conn, tenants["lab"], limit=10)).observations

    assert fish.scientific_name is None
