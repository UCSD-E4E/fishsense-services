"""A frame's automatic mask, as bytes, for the review tool (2026-10-22).

The head and tail of a fix snap to the mask, as the app's do
(fishsense-mobile ``mask_snap.dart``), so the browser needs the mask's
pixels. A presigned link is no use for that: reading an image's pixels needs
it served from the page's own origin. So the API reads the mask through the
object store's internal endpoint and the web passes it on. Only the frame's
current prediction's mask, to a member of its tenant.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from depth_measure_seed import capture, forget_identities  # noqa: F401
from test_automatic_results_store import _in, _measured, _predicted
from test_mobile_api import (
    ALICE,
    OUTSIDER,
    _bearer,
    client,
    geocoder,
    stores,
)  # noqa: F401

from fishsense_services_api.automatic_results_store import persist_automatic_head_tails
from fishsense_services_api.prediction_masks_store import PredictionMask

PNG = b"\x89PNG\r\n\x1a\n" + b"mask bytes"


@pytest.fixture
async def lab(seed_memberships):
    found = await seed_memberships(
        {ALICE: {"lab": "member"}, OUTSIDER: {"partner": "member"}}
    )
    return found["lab"]


async def _masked(owner_engine, app_engine, lab, stores) -> uuid.UUID:
    mission_id, _ = await _measured(owner_engine, app_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    objects = stores.for_tenant("lab")
    key = f"masks/automatic/{c}/m.png"
    objects._internal.put_object(Bucket=objects.bucket, Key=key, Body=PNG)
    mask = PredictionMask(
        capture_id=c, bucket=objects.bucket, key=key, sha256="ab" * 32,
        size=len(PNG), content_type="image/png", width=4014, height=3016,
    )  # fmt: skip
    await _in(app_engine, lab, persist_automatic_head_tails, mission_id,
              [_predicted(c)], [mask])  # fmt: skip
    return c


async def _mask(client: httpx.AsyncClient, c, sub=ALICE) -> httpx.Response:
    return await client.get(
        f"/tenants/lab/captures/{c}/automatic/mask", headers=_bearer(sub)
    )


async def test_a_member_gets_the_frames_mask_bytes(
    client, lab, stores, owner_engine, app_engine
):
    c = await _masked(owner_engine, app_engine, lab, stores)

    response = await _mask(client, c)

    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=3600"


async def test_a_frame_with_no_mask_is_404(client, lab, owner_engine, app_engine):
    _, c = await _measured(owner_engine, app_engine, lab)

    assert (await _mask(client, c)).status_code == 404


async def test_another_tenant_cannot_read_it(
    client, lab, stores, owner_engine, app_engine
):
    c = await _masked(owner_engine, app_engine, lab, stores)

    assert (await _mask(client, c, OUTSIDER)).status_code == 404
