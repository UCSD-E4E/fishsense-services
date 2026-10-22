"""A mission's captures, measured or not (docs/design/README.md: "a capture
is every frame, measured or not"; the owner said yes, 2026-10-21).

``GET /tenants/{slug}/missions/{number}/captures`` lists every canonical
capture of the mission in the order it was taken, each with its photo (a
phone's own JPEG; a Lite frame's render thumbnail) and its measured fish, if
any. An unmeasured capture is listed too: the Fish list shows measured fish
only, so a phone capture that was never measured is seen here or nowhere.
"""

from __future__ import annotations

from datetime import timedelta

from depth_measure_seed import capture, exec_, forget_identities  # noqa: F401
from sqlalchemy import text
from test_observations_api import OUTSIDER, _get, client  # noqa: F401
from test_phone_missions import _capture as _phone, _commit

from observation_seed import ALICE, lite, render, thumbnail


async def _tenants(seed_memberships):
    return await seed_memberships(
        {ALICE: {"lab": "member"}, OUTSIDER: {"partner": "member"}}
    )


async def _mission(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("""
                    SELECT m.id, m.number FROM captures c
                    JOIN missions m ON m.id = c.mission_id WHERE c.id = :c
                    """),
                {"c": capture_id},
            )
        ).one()


async def test_a_mission_lists_every_capture_measured_or_not(
    client, seed_memberships, owner_engine
):
    lab = (await _tenants(seed_memberships))["lab"]
    measured, fish = await lite(owner_engine, lab)
    mission = await _mission(owner_engine, measured)
    unmeasured = await capture(owner_engine, lab, mission.id)

    body = (
        await _get(client, f"/tenants/lab/missions/{mission.number}/captures")
    ).json()

    listed = {c["capture_id"]: c for c in body["captures"]}
    assert set(listed) == {str(measured), str(unmeasured)}
    assert listed[str(measured)]["fish"] == [{"fish_id": str(fish), "length_m": 0.305}]
    assert listed[str(unmeasured)]["fish"] == []
    assert body["next"] is None


async def test_they_come_in_the_order_they_were_taken(
    client, seed_memberships, owner_engine
):
    lab = (await _tenants(seed_memberships))["lab"]
    measured, _ = await lite(owner_engine, lab)
    mission = await _mission(owner_engine, measured)
    earlier = await capture(owner_engine, lab, mission.id)
    async with owner_engine.begin() as conn:
        taken = (
            await conn.execute(
                text("SELECT captured_at FROM captures WHERE id = :c"), {"c": measured}
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE captures SET captured_at = :at WHERE id = :c"),
            {"at": taken - timedelta(minutes=5), "c": earlier},
        )

    body = (
        await _get(client, f"/tenants/lab/missions/{mission.number}/captures")
    ).json()

    assert [c["capture_id"] for c in body["captures"]] == [str(earlier), str(measured)]


async def test_a_lite_frames_photo_is_its_render_thumbnail(
    client, seed_memberships, owner_engine
):
    lab = (await _tenants(seed_memberships))["lab"]
    measured, _ = await lite(owner_engine, lab)
    mission = await _mission(owner_engine, measured)
    key = await thumbnail(owner_engine, lab, await render(owner_engine, lab, measured))

    (card,) = (
        await _get(client, f"/tenants/lab/missions/{mission.number}/captures")
    ).json()["captures"]

    assert f"/fishsense-tenant-lab/{key}" in card["photo_url"]


async def test_a_phone_capture_never_measured_is_listed_with_its_photo(
    client, seed_memberships, owner_engine, app_engine
):
    lab = (await _tenants(seed_memberships))["lab"]
    async with owner_engine.connect() as conn:
        alice = (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": ALICE}
            )
        ).scalar_one()
    phone = _phone(alice)
    committed = await _commit(app_engine, lab, phone)
    mission = await _mission(owner_engine, committed.capture_id)

    (card,) = (
        await _get(client, f"/tenants/lab/missions/{mission.number}/captures")
    ).json()["captures"]

    assert card["fish"] == []
    assert card["client_capture_uuid"] == str(phone.client_capture_uuid)
    assert f"/{phone.rgb.key}" in card["photo_url"]


async def test_the_list_pages(client, seed_memberships, owner_engine):
    lab = (await _tenants(seed_memberships))["lab"]
    measured, _ = await lite(owner_engine, lab)
    mission = await _mission(owner_engine, measured)
    await capture(owner_engine, lab, mission.id)
    path = f"/tenants/lab/missions/{mission.number}/captures"

    first = (await _get(client, path, limit=1)).json()
    rest = (await _get(client, path, limit=1, after=first["next"])).json()

    assert len(first["captures"]) == 1 and first["next"]
    assert len(rest["captures"]) == 1 and rest["next"] is None
    assert first["captures"][0]["capture_id"] != rest["captures"][0]["capture_id"]


async def test_a_mission_the_caller_cant_see_is_404(
    client, seed_memberships, owner_engine
):
    lab = (await _tenants(seed_memberships))["lab"]
    measured, _ = await lite(owner_engine, lab)
    mission = await _mission(owner_engine, measured)

    outsider = await _get(
        client, f"/tenants/lab/missions/{mission.number}/captures", sub=OUTSIDER
    )
    unknown = await _get(client, "/tenants/lab/missions/99999/captures")

    assert (outsider.status_code, unknown.status_code) == (404, 404)
