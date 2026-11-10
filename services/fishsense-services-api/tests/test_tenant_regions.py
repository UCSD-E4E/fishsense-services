"""Where a tenant's fish were measured, by broad region, for its Today map.

Coarse on purpose (the owner, 2026-10-21): the map is for a picture of where
the work happens, and no marker is ever finer than a region. Wild fish only:
fish models, the decoys of pool trials, are left off (the owner, the same
day), as they are on the public map. Pools are on it
too, at their region (the owner: "Pool" is in the Florida Keys, "Nathan's
Pool" in Southern California). A fish is placed by its mission's folder,
else by its phone's GPS (the nearest region within 300 km); anything else is
"elsewhere".
"""

from __future__ import annotations

import itertools

import pytest
from depth_measure_seed import exec_, forget_identities  # noqa: F401
from observation_seed import ALICE, lite, mobile

from fishsense_services_api import regions
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.tenant_regions_store import tenant_regions

KEYS = "Florida Keys, USA"
SOCAL = "Southern California, USA"
CARIBBEAN = "Caribbean"


class TestRegionRules:
    @pytest.mark.parametrize(
        "place, region",
        [
            ("Islamorada", KEYS),
            ("Key Largo", KEYS),
            ("Qui Lagoon", KEYS),  # the owner, 2026-11-09
            ("Cuba", CARIBBEAN),
            ("San Diego", SOCAL),
            ("Pool", KEYS),
            ("Nathan's Pool", SOCAL),
            ("Atlantis", None),
            (None, None),
        ],
    )
    def test_a_place_belongs_to_its_region(self, place, region):
        assert regions.region_of_place(place) == region

    def test_the_public_page_still_leaves_pools_out(self):
        assert regions.region_of_place("Pool", pools=False) is None
        assert regions.region_of_place("Islamorada", pools=False) == KEYS

    def test_gps_joins_the_nearest_region_within_300_km(self):
        assert regions.nearest_region(32.8665, -117.2546) == SOCAL  # La Jolla
        assert regions.nearest_region(24.55, -81.78) == KEYS  # Key West
        assert regions.nearest_region(48.85, 2.35) is None  # Paris


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member", "partner": "member"}})


_devices = itertools.count(10)


async def _filed(owner_engine, tenant_id, folder: str, *, model=None) -> None:
    """A measured Lite fish whose mission sits in ``folder``: a wild one, or
    a fish ``model`` (name, known length)."""
    capture_id, _ = await lite(
        owner_engine,
        tenant_id,
        species=None if model else ("A b", "C"),
        model=model,
        device_name=f"FSL-{next(_devices)}",
    )
    await exec_(
        owner_engine,
        "UPDATE missions SET source_path = :p WHERE id = "
        "(SELECT mission_id FROM captures WHERE id = :c)",
        p=f"/nas/{folder}/FSL-07 dive",
        c=capture_id,
    )


async def _regions(app_engine, tenant_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await tenant_regions(conn, tenant_id)


class TestTenantRegions:
    async def test_fish_are_counted_at_their_missions_region(
        self, owner_engine, app_engine, tenants
    ):
        lab = tenants["lab"]
        await _filed(owner_engine, lab, "102724_Islamorada")
        await _filed(owner_engine, lab, "Alligator Reef")
        await _filed(owner_engine, lab, "Pool Calibration")
        await _filed(owner_engine, lab, "Nathans Pool")

        found = await _regions(app_engine, lab)

        assert {r.name: r.fish for r in found.regions} == {KEYS: 3, SOCAL: 1}
        assert found.regions[0].name == KEYS  # most first
        assert (found.regions[0].latitude, found.regions[0].longitude) == (
            regions.REGIONS[KEYS]
        )
        assert found.elsewhere == 0

    async def test_fish_models_are_left_off_the_map(
        self, owner_engine, app_engine, tenants
    ):
        """Pool trials are fish models: they are neither on the map nor
        "elsewhere"."""
        lab = tenants["lab"]
        await _filed(owner_engine, lab, "102724_Islamorada")
        await _filed(owner_engine, lab, "Alligator Reef", model=("Ginny", 0.3127))
        await _filed(owner_engine, lab, "Nathans Pool", model=("George", 0.3127))

        found = await _regions(app_engine, lab)

        assert {r.name: r.fish for r in found.regions} == {KEYS: 1}
        assert found.elsewhere == 0

    async def test_a_phone_fish_is_placed_by_its_gps(
        self, owner_engine, app_engine, tenants
    ):
        lab = tenants["lab"]
        await mobile(app_engine, owner_engine, lab)  # La Jolla
        far, _ = await mobile(app_engine, owner_engine, lab)
        await exec_(
            owner_engine,
            "UPDATE captures SET latitude = 48.85, longitude = 2.35 WHERE id = :c",
            c=far,
        )  # Paris

        found = await _regions(app_engine, lab)

        assert {r.name: r.fish for r in found.regions} == {SOCAL: 1}
        assert found.elsewhere == 1

    async def test_a_fish_with_no_place_is_elsewhere(
        self, owner_engine, app_engine, tenants
    ):
        await lite(owner_engine, tenants["lab"], species=("A b", "C"))

        found = await _regions(app_engine, tenants["lab"])

        assert (found.regions, found.elsewhere) == ([], 1)

    async def test_only_the_tenants_own_fish_count(
        self, owner_engine, app_engine, tenants
    ):
        await _filed(owner_engine, tenants["partner"], "Islamorada")

        found = await _regions(app_engine, tenants["lab"])

        assert (found.regions, found.elsewhere) == ([], 0)


async def _automatic(owner_engine, app_engine, lab, folder: str | None):
    """A frame with a current automatic length, its mission in ``folder``."""
    from test_automatic_results_store import _measured

    mission_id, c = await _measured(owner_engine, app_engine, lab)
    if folder is not None:
        await exec_(
            owner_engine,
            "UPDATE missions SET source_path = :p WHERE id = :m",
            p=f"/nas/{folder}/FSL-{next(_devices)} dive",
            m=mission_id,
        )
    return mission_id, c


async def _automatic_regions(app_engine, tenant_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await tenant_regions(conn, tenant_id, source="automatic")


class TestAutomaticRegions:
    """The map from the automatic lengths instead (the owner, 2026-11-08: a
    toggle between the human and the automatic labels). Unvalidated: the
    page says so. The lengths are the automatic lengths list's
    (`automatic_lengths_store`): current ones, no slate frame."""

    async def test_lengths_are_counted_at_their_missions_region(
        self, owner_engine, app_engine
    ):
        from depth_measure_seed import tenant

        lab = await tenant(owner_engine)
        await _automatic(owner_engine, app_engine, lab, "102724_Islamorada")
        await _automatic(owner_engine, app_engine, lab, "Alligator Reef")
        await _automatic(owner_engine, app_engine, lab, "Nathans Pool")
        await _automatic(owner_engine, app_engine, lab, None)

        found = await _automatic_regions(app_engine, lab)

        assert {r.name: r.fish for r in found.regions} == {KEYS: 2, SOCAL: 1}
        assert found.regions[0].name == KEYS
        assert found.elsewhere == 1

    async def test_the_human_map_does_not_count_them(self, owner_engine, app_engine):
        from depth_measure_seed import tenant

        lab = await tenant(owner_engine)
        await _automatic(owner_engine, app_engine, lab, "Islamorada")

        found = await _regions(app_engine, lab)

        assert (found.regions, found.elsewhere) == ([], 0)

    async def test_a_frame_with_gps_is_placed_by_it(self, owner_engine, app_engine):
        from depth_measure_seed import tenant

        lab = await tenant(owner_engine)
        _, c = await _automatic(owner_engine, app_engine, lab, None)
        await exec_(
            owner_engine,
            "UPDATE captures SET latitude = 32.8665, longitude = -117.2546 "
            "WHERE id = :c",
            c=c,
        )  # La Jolla

        found = await _automatic_regions(app_engine, lab)

        assert {r.name: r.fish for r in found.regions} == {SOCAL: 1}

    async def test_a_slate_frame_is_left_off(self, owner_engine, app_engine):
        from test_slate_presence_store import _row as _slate_row

        from depth_measure_seed import tenant
        from fishsense_services_api.slate_presence_store import (
            persist_slate_presence,
        )

        lab = await tenant(owner_engine)
        mission_id, c = await _automatic(owner_engine, app_engine, lab, "Islamorada")
        async with tenant_transaction(app_engine, lab) as conn:
            await persist_slate_presence(conn, lab, mission_id, [_slate_row(c)])

        found = await _automatic_regions(app_engine, lab)

        assert (found.regions, found.elsewhere) == ([], 0)

    async def test_a_stale_length_is_left_off(self, owner_engine, app_engine):
        """A newer head/tail makes the length stale: it isn't current."""
        from test_automatic_results_store import _in, _predict, _predicted

        from depth_measure_seed import tenant

        lab = await tenant(owner_engine)
        mission_id, c = await _automatic(owner_engine, app_engine, lab, "Islamorada")
        await _predict(app_engine, lab, mission_id, _predicted(c, head_x=1700.0))

        found = await _automatic_regions(app_engine, lab)

        assert (found.regions, found.elsewhere) == ([], 0)

    async def test_a_frame_a_person_labeled_a_fish_model_is_left_off(
        self, owner_engine, app_engine
    ):
        """As on the human map: the decoys of pool trials are not wild fish."""
        from depth_measure_seed import calibrate, fish, measurement, tenant

        lab = await tenant(owner_engine)
        mission_id, c = await _automatic(owner_engine, app_engine, lab, "Islamorada")
        await _automatic(owner_engine, app_engine, lab, "Islamorada")
        cal = await calibrate(owner_engine, lab, mission_id)
        await measurement(
            owner_engine, lab, c, await fish(owner_engine, lab, model="Ginny"), cal
        )

        found = await _automatic_regions(app_engine, lab)

        assert {r.name: r.fish for r in found.regions} == {KEYS: 1}


async def _mapped(app_engine, lab) -> int:
    """Every automatic length on the map: in a region or elsewhere."""
    found = await _automatic_regions(app_engine, lab)
    return sum(r.fish for r in found.regions) + found.elsewhere


class TestAutomaticRegionsAreTheCurrentLengths:
    """The map restates `current_automatic_measurements`'s rules rather than
    read the view (2026-11-09: through the view, the lab's map ran past the
    app role's 30 s statement timeout, and the page showed a 500; over 120 s
    by hand, against 5.7 s restated). These pin each rule; on production the
    restatement picked the view's row on all 27,313 of the lab's current
    frames."""

    async def test_a_frame_measured_twice_counts_once(self, owner_engine, app_engine):
        from test_automatic_results_store import _in

        from depth_measure_seed import tenant
        from fishsense_services_api.automatic_results_store import (
            AutomaticMeasurementRow,
            persist_automatic_measurements,
        )

        lab = await tenant(owner_engine)
        mission_id, c = await _automatic(owner_engine, app_engine, lab, "Islamorada")
        (old,) = await _rows(owner_engine, "SELECT * FROM automatic_measurements")
        await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
            AutomaticMeasurementRow(
                capture_id=c,
                automatic_head_tail_prediction_id=old["automatic_head_tail_prediction_id"],
                automatic_keypoint_prediction_id=old["automatic_keypoint_prediction_id"],
                calibration_source=old["calibration_source"],
                automatic_laser_calibration_id=old["automatic_laser_calibration_id"],
                laser_calibration_id=old["laser_calibration_id"],
                camera_calibration_id=old["camera_calibration_id"],
                length_m=0.43, depth_m=2.1, algorithm="laser_depth_fronto_parallel",
                algorithm_version="1", core_version="4.1.0",
            )
        ])  # fmt: skip

        assert await _mapped(app_engine, lab) == 1

    async def test_a_length_on_a_superseded_calibration_is_left_off(
        self, owner_engine, app_engine
    ):
        from test_automatic_results_store import _in, _label_free

        from depth_measure_seed import tenant
        from fishsense_services_api.automatic_results_store import (
            persist_automatic_calibration,
        )

        lab = await tenant(owner_engine)
        mission_id, _ = await _automatic(owner_engine, app_engine, lab, "Islamorada")
        await _in(app_engine, lab, persist_automatic_calibration, mission_id,
                  _label_free())  # fmt: skip

        assert await _mapped(app_engine, lab) == 0

    async def test_a_length_off_its_frames_beam_aim_is_left_off(
        self, owner_engine, app_engine
    ):
        """A frame measured with the first aim's beam, then found to be after
        the laser moved, has no current length."""
        from _slate_calibration_seed import beam_moved_after
        from test_automatic_results_store import _backlog_mission
        from test_calibration_per_aim import (
            BEAM,
            _dots,
            _frames,
            _length,
            _measure,
            _persist,
        )

        from depth_measure_seed import tenant

        lab = await tenant(owner_engine)
        mission_id = await _backlog_mission(owner_engine, lab)
        first, later = await _frames(owner_engine, app_engine, lab, mission_id,
                                     _dots(BEAM, 2))  # fmt: skip
        await _persist(app_engine, lab, mission_id)
        inputs = await _measure(app_engine, lab, mission_id)
        for c in (first, later):
            await _length(app_engine, lab, mission_id, inputs, c)
        assert await _mapped(app_engine, lab) == 2

        await beam_moved_after(owner_engine, first)

        assert await _mapped(app_engine, lab) == 1

    def test_the_map_does_not_read_the_current_lengths_view(self):
        from fishsense_services_api import tenant_regions_store

        assert "current_automatic_measurements" not in (
            tenant_regions_store._AUTOMATIC_SQL
        )

    async def test_the_view_it_restates_is_unchanged(self, owner_engine):
        """A migration that changes `current_automatic_measurements` must
        change the map's restatement too (`tenant_regions_store`), then
        update this pin."""
        import hashlib

        from sqlalchemy import text

        async with owner_engine.connect() as conn:
            definition = (
                await conn.execute(
                    text(
                        "SELECT pg_get_viewdef("
                        "'current_automatic_measurements'::regclass, true)"
                    )
                )
            ).scalar_one()
        assert hashlib.sha256(definition.encode()).hexdigest() == VIEW_SHA256


async def _rows(engine, sql, **params):
    from test_automatic_results_store import _rows as rows

    return await rows(engine, sql, **params)


#: sha256 of `current_automatic_measurements`'s definition (0086: the rules
#: unchanged; the pick asked per length, keyed by mission).
VIEW_SHA256 = "f7140c53178aa34c5906cfa9cccf175f3024d1b711a1200d1f12d7f0a9459642"
