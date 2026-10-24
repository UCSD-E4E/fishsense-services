"""Automatic lengths over HTTP (the owner, 2026-10-24): a tenant's, or one
mission's, a page at a time, for any member, as automatic and unvalidated.

``GET /tenants/{slug}/automatic/lengths`` and
``GET /tenants/{slug}/missions/{number}/automatic/lengths``. The rules are in
``automatic_lengths_store`` and its tests.
"""

from __future__ import annotations

import pytest
from depth_measure_seed import forget_identities  # noqa: F401
from test_automatic_lengths_store import _species
from test_automatic_results_store import _measured
from test_automatic_review_api import _lab, _number
from test_observations_api import OUTSIDER, _get, client  # noqa: F401


async def test_a_member_reads_the_tenants_lengths(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _species(owner_engine, app_engine, lab, mission_id, c)

    response = await _get(client, "/tenants/lab/automatic/lengths")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next"] is None
    (row,) = body["lengths"]
    assert row["capture_id"] == str(c)
    assert row["mission_number"] == await _number(owner_engine, mission_id)
    assert row["length_m"] == 0.42
    assert row["scientific_name"] == "Lachnolaimus maximus"
    assert row["human_length_m"] is None
    assert row["mission_human_measured"] is False
    assert row["reviews"] == {}


async def test_one_missions_lengths(client, seed_memberships, owner_engine, app_engine):
    lab = await _lab(seed_memberships)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _measured(owner_engine, app_engine, lab)
    number = await _number(owner_engine, mission_id)

    body = (
        await _get(client, f"/tenants/lab/missions/{number}/automatic/lengths")
    ).json()

    assert [r["capture_id"] for r in body["lengths"]] == [str(c)]


async def test_by_species(client, seed_memberships, owner_engine, app_engine):
    lab = await _lab(seed_memberships)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _measured(owner_engine, app_engine, lab)
    await _species(owner_engine, app_engine, lab, mission_id, c)

    body = (
        await _get(
            client, "/tenants/lab/automatic/lengths", species="Lachnolaimus maximus"
        )
    ).json()

    assert [r["capture_id"] for r in body["lengths"]] == [str(c)]


async def test_pages(client, seed_memberships, owner_engine, app_engine):
    lab = await _lab(seed_memberships)
    for _ in range(3):
        await _measured(owner_engine, app_engine, lab)

    first = (await _get(client, "/tenants/lab/automatic/lengths", limit=2)).json()
    second = (
        await _get(
            client, "/tenants/lab/automatic/lengths", limit=2, after=first["next"]
        )
    ).json()

    assert len(first["lengths"]) == 2 and first["next"]
    assert len(second["lengths"]) == 1 and second["next"] is None


@pytest.mark.parametrize(
    "query", [{"after": "nope"}, {"limit": 0}, {"limit": 501}], ids=str
)
async def test_a_bad_page_is_422(client, seed_memberships, query):
    await _lab(seed_memberships)

    response = await _get(client, "/tenants/lab/automatic/lengths", **query)

    assert response.status_code == 422


async def test_an_unknown_mission_is_404(client, seed_memberships):
    await _lab(seed_memberships)

    response = await _get(client, "/tenants/lab/missions/99999/automatic/lengths")

    assert response.status_code == 404


async def test_someone_outside_the_tenant_sees_nothing(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    number = await _number(owner_engine, mission_id)

    tenant = await _get(client, "/tenants/lab/automatic/lengths", sub=OUTSIDER)
    mission = await _get(
        client, f"/tenants/lab/missions/{number}/automatic/lengths", sub=OUTSIDER
    )

    assert (tenant.status_code, mission.status_code) == (404, 404)
