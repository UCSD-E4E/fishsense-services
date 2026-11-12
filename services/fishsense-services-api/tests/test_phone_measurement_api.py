"""A phone capture's server measurement, for the web (the owner, 2026-11-10:
the CCFRP lengths shown for a presentation, unvalidated).

``GET /tenants/{slug}/captures/{capture_id}/phone-measurement`` is the
capture's current server result (`current_phone_measurements`): its status,
length, snout and fork, their depths, and what measured it; the capture's
JPEG and the mask as presigned links. ``.../phone-measurement/mask`` is the
mask's bytes, which the web serves from its own origin to tint the fish. A
mission's capture cards carry the current phone result too, so a mission page
shows a length on a capture nobody measured. Read-only, for any member;
another tenant's capture is 404.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import text
from test_mobile_api import (  # noqa: F401
    ALICE,
    OUTSIDER,
    _bearer,
    client,
    geocoder,
    stores,
)
from test_phone_measurement_store import _measured
from test_phone_missions import _capture, _commit

from depth_measure_seed import forget_identities  # noqa: F401
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.phone_measurement_store import record_phone_measurements

PNG = b"\x89PNG\r\n\x1a\n" + b"phone mask bytes"


@pytest.fixture
async def lab(seed_memberships):
    found = await seed_memberships(
        {ALICE: {"lab": "member"}, OUTSIDER: {"partner": "member"}}
    )
    return found["lab"]


async def _alice(owner_engine) -> uuid.UUID:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": ALICE}
            )
        ).scalar_one()


async def _phone(owner_engine, app_engine, lab):
    phone = _capture(await _alice(owner_engine))
    committed = await _commit(app_engine, lab, phone)
    return committed.capture_id, phone


async def _in_bucket(owner_engine, capture_id, bucket: str) -> None:
    """The capture's files in the test store's bucket (the seed names the
    production one): a link is made only for the tenant's current bucket."""
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE capture_objects SET bucket = :b WHERE capture_id = :c"),
            {"b": bucket, "c": capture_id},
        )


async def _record(app_engine, lab, *results):
    async with tenant_transaction(app_engine, lab) as conn:
        await record_phone_measurements(conn, lab, list(results))


def _masked(capture_id, stores):
    objects = stores.for_tenant("lab")
    result = _measured(capture_id)
    mask = replace(result.mask, bucket=objects.bucket, size=len(PNG))
    objects._internal.put_object(Bucket=objects.bucket, Key=mask.key, Body=PNG)
    return replace(result, mask=mask)


async def _get(client: httpx.AsyncClient, path: str, sub=ALICE) -> httpx.Response:
    return await client.get(path, headers=_bearer(sub))


async def test_a_member_gets_the_captures_current_phone_measurement(
    client, lab, stores, owner_engine, app_engine
):
    c, phone = await _phone(owner_engine, app_engine, lab)
    await _in_bucket(owner_engine, c, stores.for_tenant("lab").bucket)
    await _record(app_engine, lab, _measured(c, length_m=0.250))
    await _record(app_engine, lab, _masked(c, stores))  # the newer: current

    response = await _get(client, f"/tenants/lab/captures/{c}/phone-measurement")

    assert response.status_code == 200, response.text
    found = response.json()
    assert found["status"] == "measured" and found["length_m"] == 0.284
    assert found["snout"] == {"x": 530.0, "y": 690.0}
    assert found["fork"] == {"x": 1500.0, "y": 740.0}
    assert (found["snout_depth_m"], found["fork_depth_m"]) == (0.38, 0.44)
    assert (found["mask_width"], found["mask_height"]) == (1920, 1440)
    assert found["model_id"] == "fishial/1@f721445f37db"
    assert found["algorithm"] == "fishsense-mobile@cd120f4"
    assert f"/{phone.rgb.key}" in found["photo_url"]
    assert "/masks/phone/" in found["mask_url"]


async def test_no_fish_has_no_length_points_or_mask(
    client, lab, owner_engine, app_engine
):
    c, _ = await _phone(owner_engine, app_engine, lab)
    await _record(app_engine, lab, _measured(
        c, status="no_fish", length_m=None, snout_x=None, snout_y=None, fork_x=None,
        fork_y=None, snout_depth_m=None, fork_depth_m=None, mask=None,
    ))  # fmt: skip

    found = (await _get(client, f"/tenants/lab/captures/{c}/phone-measurement")).json()

    assert found["status"] == "no_fish"
    assert (found["length_m"], found["snout"], found["fork"], found["mask_url"]) == (
        None,
        None,
        None,
        None,
    )


async def test_a_capture_never_measured_by_the_server_is_404(
    client, lab, owner_engine, app_engine
):
    c, _ = await _phone(owner_engine, app_engine, lab)

    response = await _get(client, f"/tenants/lab/captures/{c}/phone-measurement")

    assert response.status_code == 404


async def test_another_tenant_cannot_read_it(
    client, lab, stores, owner_engine, app_engine
):
    c, _ = await _phone(owner_engine, app_engine, lab)
    await _record(app_engine, lab, _masked(c, stores))

    for path in (f"/tenants/partner/captures/{c}/phone-measurement",
                 f"/tenants/partner/captures/{c}/phone-measurement/mask"):  # fmt: skip
        assert (await _get(client, path, OUTSIDER)).status_code == 404


async def test_the_mask_is_served_as_its_bytes(
    client, lab, stores, owner_engine, app_engine
):
    c, _ = await _phone(owner_engine, app_engine, lab)
    await _record(app_engine, lab, _masked(c, stores))

    response = await _get(client, f"/tenants/lab/captures/{c}/phone-measurement/mask")

    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"


async def test_no_mask_is_404(client, lab, owner_engine, app_engine):
    c, _ = await _phone(owner_engine, app_engine, lab)
    await _record(app_engine, lab, _measured(c, mask=None))

    response = await _get(client, f"/tenants/lab/captures/{c}/phone-measurement/mask")

    assert response.status_code == 404


async def _mission_number(owner_engine, capture_id) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("""
                    SELECT m.number FROM captures c
                    JOIN missions m ON m.id = c.mission_id WHERE c.id = :c
                    """),
                {"c": capture_id},
            )
        ).scalar_one()


async def test_a_missions_capture_card_carries_its_phone_result(
    client, lab, owner_engine, app_engine
):
    c, _ = await _phone(owner_engine, app_engine, lab)
    number = await _mission_number(owner_engine, c)
    path = f"/tenants/lab/missions/{number}/captures"

    (before,) = (await _get(client, path)).json()["captures"]
    await _record(app_engine, lab, _measured(c))
    (after,) = (await _get(client, path)).json()["captures"]

    assert before["phone"] is None
    assert after["phone"] == {"status": "measured", "length_m": 0.284}
    assert after["fish"] == []  # unvalidated: never a measured fish


async def test_a_photo_outside_the_tenants_current_bucket_gets_no_link(
    client, lab, owner_engine, app_engine
):
    """The seed's files name a bucket the tenant's store isn't: no link to it."""
    c, _ = await _phone(owner_engine, app_engine, lab)
    await _record(app_engine, lab, _measured(c, mask=None))

    found = (await _get(client, f"/tenants/lab/captures/{c}/phone-measurement")).json()

    assert found["status"] == "measured" and found["photo_url"] is None


async def test_a_mask_recorded_in_another_bucket_is_not_served(
    client, lab, stores, owner_engine, app_engine
):
    """Only the tenant's current bucket: a row naming another is 404, even
    where the same key exists in the current one."""
    c, _ = await _phone(owner_engine, app_engine, lab)
    result = _masked(c, stores)
    await _record(app_engine, lab, replace(
        result, mask=replace(result.mask, bucket="fishsense-tenant-elsewhere")
    ))  # fmt: skip

    response = await _get(client, f"/tenants/lab/captures/{c}/phone-measurement/mask")

    assert response.status_code == 404


async def test_the_fish_list_carries_the_server_length_as_automatic(
    client, lab, owner_engine, app_engine
):
    """The owner, 2026-11-11: every fish on the Fish page, with its trust."""
    c, _ = await _phone(owner_engine, app_engine, lab)
    await _record(app_engine, lab, _measured(c))

    found = (await _get(client, "/tenants/lab/observations")).json()

    (fish,) = found["observations"]
    assert (fish["capture_id"], fish["length_m"]) == (str(c), 0.284)
    assert (fish["trust"], fish["length_sd_m"]) == ("automatic", None)
    assert fish["mission"]["number"] is not None
