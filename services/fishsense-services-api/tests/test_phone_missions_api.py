"""A phone mission over HTTP (0057): what it is, and no camera port for it."""

from sqlalchemy import text
from test_missions_api import ADMIN, _bearer, client, tenants  # noqa: F401
from test_phone_missions import _capture, _commit, _mission_of


async def _phone_mission(app_engine, owner_engine, tenants) -> int:
    async with owner_engine.connect() as conn:
        admin = (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": ADMIN}
            )
        ).scalar_one()
    committed = await _commit(app_engine, tenants["lab"], _capture(admin))
    return (await _mission_of(owner_engine, committed.capture_id)).number


async def test_its_page_says_it_is_a_phones_and_has_no_folder(
    client, tenants, app_engine, owner_engine
):
    number = await _phone_mission(app_engine, owner_engine, tenants)

    body = (
        await client.get(f"/tenants/lab/missions/{number}", headers=_bearer(ADMIN))
    ).json()

    assert (body["source"], body["source_path"]) == ("mobile", None)
    assert body["name"] == "2026.10.11.FishSense.La Jolla Cove"


async def test_the_list_says_which_missions_are_phones(
    client, tenants, app_engine, owner_engine
):
    number = await _phone_mission(app_engine, owner_engine, tenants)

    listed = (await client.get("/tenants/lab/missions", headers=_bearer(ADMIN))).json()

    assert {m["number"]: m["source"] for m in listed} == {number: "mobile"}


async def test_a_phone_has_no_camera_port_to_state(
    client, tenants, app_engine, owner_engine
):
    number = await _phone_mission(app_engine, owner_engine, tenants)

    response = await client.put(
        f"/tenants/lab/missions/{number}/port",
        headers=_bearer(ADMIN),
        json={"port": "flat"},
    )

    assert response.status_code == 409
    assert "phone" in response.json()["detail"]
