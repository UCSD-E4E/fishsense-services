"""Observations: every measured fish, Lite and Mobile alike (PLAN.md §4.9).

Lite and Mobile are the same thing -- a device photographs a fish and a
length comes from the image plus a depth source -- so the API and the web
read one model of it: the ``observations`` view (migration 0045), one row per
current measurement, whichever device took the photo. What differs is how
the depth was found, and that is a column (``method``: ``laser`` for Lite's
server measurements, ``lidar`` for a phone's), not a separate table.

Read as the app role, under RLS, like every store.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from depth_measure_seed import T0, exec_, forget_identities  # noqa: F401
from sqlalchemy import text
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.observation_store import (
    list_observations,
    observation,
)
from observation_seed import ALICE, lite as _lite, mobile as _mobile


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member", "partner": "member"}})


async def _list(app_engine, tenant_id, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await list_observations(
            conn, tenant_id, limit=kwargs.pop("limit", 50), **kwargs
        )


class TestList:
    async def test_a_measured_lite_capture_is_an_observation_by_laser(
        self, owner_engine, app_engine, tenants
    ):
        capture_id, fish_id = await _lite(
            owner_engine, tenants["lab"], species=("Lachnolaimus maximus", "Hogfish")
        )

        [found] = await _list(app_engine, tenants["lab"])

        assert (found.capture_id, found.fish_id) == (capture_id, fish_id)
        assert found.method == "laser"
        assert found.device_kind == "lite"
        assert found.device_name == "FSL-07"
        assert found.dive_name == "Alligator Deep"
        assert found.dive_number is not None
        assert (found.scientific_name, found.common_name) == (
            "Lachnolaimus maximus",
            "Hogfish",
        )
        assert found.length_m == pytest.approx(0.305)
        assert found.photo_key is None

    async def test_a_mobile_capture_is_an_observation_by_lidar(
        self, owner_engine, app_engine, tenants
    ):
        capture_id, client_uuid = await _mobile(
            app_engine, owner_engine, tenants["lab"]
        )

        [found] = await _list(app_engine, tenants["lab"])

        assert found.capture_id == capture_id
        assert found.method == "lidar"
        assert found.device_kind == "mobile"
        assert found.client_capture_uuid == client_uuid
        assert found.place_name == "Scripps Pier"
        assert (found.latitude, found.longitude) == (32.8665, -117.2546)
        assert found.length_m == pytest.approx(0.42)
        assert found.photo_key == f"mobile/captures/{client_uuid}/rgb_jpeg"
        assert found.dive_number is None

    async def test_both_are_listed_together_newest_first_and_filter_by_device(
        self, owner_engine, app_engine, tenants
    ):
        lite, _ = await _lite(owner_engine, tenants["lab"], species=("A b", "C"))
        phone, _ = await _mobile(
            app_engine, owner_engine, tenants["lab"], at=T0 + timedelta(days=1)
        )

        everything = await _list(app_engine, tenants["lab"])
        only_lite = await _list(app_engine, tenants["lab"], device="lite")
        only_phone = await _list(app_engine, tenants["lab"], device="mobile")

        assert [o.capture_id for o in everything] == [phone, lite]
        assert [o.capture_id for o in only_lite] == [lite]
        assert [o.capture_id for o in only_phone] == [phone]

    async def test_a_fish_model_shows_its_name_and_known_length(
        self, owner_engine, app_engine, tenants
    ):
        await _lite(owner_engine, tenants["lab"], model=("Ginny", 0.3127))

        [found] = await _list(app_engine, tenants["lab"])

        assert found.fish_model == "Ginny"
        assert found.known_length_m == pytest.approx(0.3127)
        assert found.scientific_name is None

    async def test_a_capture_that_is_not_canonical_is_not_listed(
        self, owner_engine, app_engine, tenants
    ):
        await _lite(owner_engine, tenants["lab"], species=("A b", "C"), canonical=False)

        assert await _list(app_engine, tenants["lab"]) == []

    async def test_another_tenants_fish_are_not_visible(
        self, owner_engine, app_engine, tenants
    ):
        await _lite(owner_engine, tenants["partner"], species=("A b", "C"))
        await _mobile(app_engine, owner_engine, tenants["partner"])

        assert await _list(app_engine, tenants["lab"]) == []

    async def test_pages_continue_where_the_last_ended(
        self, owner_engine, app_engine, tenants
    ):
        made = [
            (
                await _mobile(
                    app_engine, owner_engine, tenants["lab"], at=T0 + timedelta(hours=h)
                )
            )[0]
            for h in range(5)
        ]

        first = await _list(app_engine, tenants["lab"], limit=2)
        second = await _list(
            app_engine, tenants["lab"], limit=2, before=first[-1].cursor
        )
        rest = await _list(
            app_engine, tenants["lab"], limit=2, before=second[-1].cursor
        )

        listed = [o.capture_id for o in first + second + rest]
        assert listed == list(reversed(made))


class TestDetail:
    async def test_a_lite_observation_carries_its_laser_evidence(
        self, owner_engine, app_engine, tenants
    ):
        capture_id, fish_id = await _lite(
            owner_engine, tenants["lab"], model=("Ginny", 0.3127)
        )

        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            found = await observation(conn, tenants["lab"], capture_id, fish_id)

        assert found.observation.method == "laser"
        assert found.algorithm == "laser_depth_fronto_parallel"
        laser = found.laser
        assert laser.depth_m == pytest.approx(1.8)
        assert laser.calibration_producer == "slate"
        assert laser.dot == (1925.0, 1310.0)
        assert (laser.head, laser.tail) == ((1600.0, 1125.0), (2200.0, 1360.0))
        assert found.device_points is None

    async def test_a_mobile_observation_carries_the_phones_points(
        self, owner_engine, app_engine, tenants
    ):
        capture_id, _ = await _mobile(app_engine, owner_engine, tenants["lab"])
        [listed] = await _list(app_engine, tenants["lab"])

        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            found = await observation(conn, tenants["lab"], capture_id, listed.fish_id)

        assert found.observation.method == "lidar"
        points = found.device_points
        assert (points.snout, points.fork, points.origin) == (
            (100.0, 200.0),
            (900.0, 210.0),
            "manual",
        )
        assert found.laser is None

    async def test_another_tenants_observation_is_not_found(
        self, owner_engine, app_engine, tenants
    ):
        capture_id, fish_id = await _lite(
            owner_engine, tenants["partner"], species=("A b", "C")
        )

        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            assert await observation(conn, tenants["lab"], capture_id, fish_id) is None


class TestOneObservationPerFish:
    """``current_measurements`` keeps one row per source, so a fish measured
    both ways would be two rows. An observation is one fish in one capture:
    the latest measured wins (§9.13's rule for "current")."""

    async def test_a_fish_measured_two_ways_is_one_observation_the_latest(
        self, owner_engine, app_engine, tenants
    ):
        capture_id, fish_id = await _lite(
            owner_engine, tenants["lab"], species=("A b", "C")
        )
        await exec_(
            owner_engine,
            "INSERT INTO measurements (tenant_id, capture_id, fish_id, source, "
            "length_m, measured_at) VALUES (:t, :c, :f, 'device', 0.5, now() + "
            "interval '1 day')",
            t=tenants["lab"],
            c=capture_id,
            f=fish_id,
        )

        listed = await _list(app_engine, tenants["lab"])

        assert [(o.capture_id, o.method, o.length_m) for o in listed] == [
            (capture_id, "lidar", 0.5)
        ]


class TestEvidence:
    async def test_the_depth_is_the_latest_written_for_its_dot(
        self, owner_engine, app_engine, tenants
    ):
        """Two depths for one dot, written in one transaction (one now()):
        the latest written is the current one, as ``current_laser_depths``
        has it (seq), whatever their timestamps."""
        capture_id, fish_id = await _lite(
            owner_engine, tenants["lab"], species=("A b", "C")
        )
        async with owner_engine.begin() as conn:
            laser, calibration = (
                await conn.execute(
                    text(
                        "SELECT laser_label_id, laser_calibration_id FROM "
                        "measurements WHERE capture_id = :c"
                    ),
                    {"c": capture_id},
                )
            ).one()
            for depth_m in (2.5, 3.5):
                await conn.execute(
                    text(
                        "INSERT INTO laser_depths (tenant_id, capture_id, "
                        "laser_label_id, laser_calibration_id, depth_m, range_m, "
                        "residual_m, core_version, created_at) VALUES (:t, :c, :l, "
                        ":cal, :d, :d, 0, '4.1.0', '2026-01-01')"
                    ),
                    {
                        "t": tenants["lab"],
                        "c": capture_id,
                        "l": laser,
                        "cal": calibration,
                        "d": depth_m,
                    },
                )

        async with tenant_transaction(app_engine, tenants["lab"]) as conn:
            found = await observation(conn, tenants["lab"], capture_id, fish_id)

        assert found.laser.depth_m == 3.5
