"""The automatic track over HTTP, for review (the owner, 2026-10-21): any
member of the tenant may read it (results shown as automatic and unvalidated).

``GET /tenants/{slug}/captures/{capture_id}/automatic``: what the run made of
one frame, with presigned links to the render its pixels are on and its mask.
``GET /tenants/{slug}/missions/{number}/automatic``: the mission's run.
"""

from __future__ import annotations

from depth_measure_seed import exec_, forget_identities  # noqa: F401
from sqlalchemy import text
from test_automatic_review_store import _render
from test_automatic_results_store import _measured
from test_observations_api import OUTSIDER, _get, client  # noqa: F401

from observation_seed import ALICE


async def _number(owner_engine, mission_id) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT number FROM missions WHERE id = :m"), {"m": mission_id}
            )
        ).scalar_one()


async def _lab(seed_memberships):
    found = await seed_memberships(
        {ALICE: {"lab": "member"}, OUTSIDER: {"partner": "member"}}
    )
    return found["lab"]


async def test_a_member_reads_what_the_run_made_of_a_frame(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, c = await _measured(owner_engine, app_engine, lab)
    key = await _render(owner_engine, lab, c)

    response = await _get(client, f"/tenants/lab/captures/{c}/automatic")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "predicted"
    assert body["head"] == {"x": 1800.0, "y": 1500.0}
    assert body["tail"] == {"x": 2300.0, "y": 1480.0}
    assert body["laser"] == {"x": 2000.0, "y": 1500.0}
    assert body["length_m"] == 0.42
    assert f"/fishsense-tenant-lab/{key}" in body["render_url"]
    assert (body["render_width"], body["render_height"]) == (4014, 3016)
    assert body["mask_url"] is None


async def test_a_frame_the_run_never_reached_is_404(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    async with owner_engine.begin() as conn:
        other = (
            await conn.execute(
                text("""
                    INSERT INTO captures (tenant_id, mission_id, source_path,
                        captured_at, checksum, is_canonical)
                    VALUES (:t, :m, 'x/unreached.ORF', now(), :sum, true) RETURNING id
                    """),
                {"t": lab, "m": mission_id, "sum": "f" * 32},
            )
        ).scalar_one()

    response = await _get(client, f"/tenants/lab/captures/{other}/automatic")

    assert response.status_code == 404


async def test_someone_outside_the_tenant_sees_nothing(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    number = await _number(owner_engine, mission_id)

    frame = await _get(client, f"/tenants/lab/captures/{c}/automatic", sub=OUTSIDER)
    run = await _get(client, f"/tenants/lab/missions/{number}/automatic", sub=OUTSIDER)

    assert (frame.status_code, run.status_code) == (404, 404)


async def test_a_member_reads_a_missions_run(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    number = await _number(owner_engine, mission_id)

    body = (await _get(client, f"/tenants/lab/missions/{number}/automatic")).json()

    assert body["frames"] == 1
    assert body["by_status"] == {"predicted": 1}
    assert body["lengths"] == 1
    assert body["lengths_use"] == "stored"
    assert body["compared"] == 0


async def test_an_unknown_mission_is_404(client, seed_memberships):
    await _lab(seed_memberships)

    response = await _get(client, "/tenants/lab/missions/99999/automatic")

    assert response.status_code == 404


async def test_a_render_outside_the_tenants_bucket_gets_no_link(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, c = await _measured(owner_engine, app_engine, lab)
    await _render(owner_engine, lab, c)
    await exec_(
        owner_engine, "UPDATE capture_renders SET bucket = 'labels-fishsense-lite'"
    )

    body = (await _get(client, f"/tenants/lab/captures/{c}/automatic")).json()

    assert body["render_url"] is None
