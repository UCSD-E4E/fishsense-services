"""A capture's depth, confidence and masks over MCP (the owner, 2026-11-12).

The owner: "it's important we don't lose precision." So no tool re-encodes a
file: each gives the file's geometry and format, its SHA-256 as stored, and a
presigned link to the exact bytes, which an agent downloads and checks. A
depth map inlined as base64 is about 90k tokens, past what MCP clients take
(Claude Code stops near 25k), so links it is (the owner's choice).

* ``get_phone_capture_maps``: a phone capture's LiDAR depth (float32
  meters, rows padded to ``row_bytes``), its confidence (0 low, 1 medium,
  2 high) and the mask FishSense Mobile made on the phone (Fishial).
* ``get_phone_measurement_mask``: the server's phone measurement's mask
  (Fishial). Unvalidated.
* ``get_automatic_mask``: the automatic run's SAM 3 mask. Unvalidated.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from depth_measure_seed import exec_, forget_identities  # noqa: F401
from sqlalchemy import text
from test_automatic_mask_api import _masked as _sam_masked
from test_mcp import AUDIENCE, ISSUER, KEY, KID, MEMBER, OPERATOR, Agent, _token
from test_mobile_api import stores  # noqa: F401
from test_phone_measurement_api import _masked as _fishial_masked
from test_phone_measurement_api import _record
from test_phone_missions import _capture, _commit

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.mobile_capture_store import CaptureFile

MASK_W, MASK_H = 1920, 1440


@pytest.fixture
def app(app_engine, stores):  # noqa: F811
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: KEY.public_key()}),
    )
    return create_app(engine=app_engine, validator=validator, stores=stores)


@pytest.fixture
async def tenants(seed_memberships, owner_engine):
    found = await seed_memberships(
        {MEMBER: {"lab": "admin"}, OPERATOR: {"lab": "member"}}
    )
    found |= await seed_memberships({"someone": {"partner": "admin"}})
    return found


@pytest.fixture
async def agent(app, tenants) -> Agent:
    return Agent(app, await _token(app, MEMBER))


async def _user(owner_engine, sub: str) -> uuid.UUID:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT id FROM users WHERE sub = :s"), {"s": sub})
        ).scalar_one()


async def _phone(owner_engine, app_engine, lab, stores, *, mask: bool):  # noqa: F811
    """A synced phone capture, its files in the test store's bucket."""
    phone = _capture(await _user(owner_engine, MEMBER))
    if mask:
        phone = replace(
            phone,
            mask_width=MASK_W,
            mask_height=MASK_H,
            files=phone.files
            + (
                CaptureFile(
                    role="mask_u8",
                    bucket="fishsense-tenant-lab",
                    key=f"mobile/captures/{phone.client_capture_uuid}/mask_u8",
                    sha256="e" * 64,
                    size=MASK_W * MASK_H,
                    content_type="application/octet-stream",
                ),
            ),
        )
    committed = await _commit(app_engine, lab, phone)
    await exec_(
        owner_engine,
        "UPDATE capture_objects SET bucket = :b WHERE capture_id = :c",
        b=stores.for_tenant("lab").bucket,
        c=committed.capture_id,
    )
    return committed.capture_id, phone


def _file(phone, role: str) -> CaptureFile:
    return next(f for f in phone.files if f.role == role)


async def test_the_new_tools_are_listed(agent):
    names = {t.name for t in await agent.list_tools()}

    assert {
        "get_phone_capture_maps",
        "get_phone_measurement_mask",
        "get_automatic_mask",
    } <= names


class TestPhoneCaptureMaps:
    async def test_depth_confidence_and_the_phones_mask_as_stored(
        self, agent, tenants, stores, owner_engine, app_engine  # noqa: F811
    ):
        c, phone = await _phone(
            owner_engine, app_engine, tenants["lab"], stores, mask=True
        )

        found = await agent.call(
            "get_phone_capture_maps",
            slug="lab",
            client_capture_uuid=str(phone.client_capture_uuid),
        )

        assert found["capture_id"] == str(c)
        assert found["intrinsics"] == list(phone.intrinsics)
        assert found["links_expire_in_s"] == 3600
        depth = found["depth"]
        assert (depth["width"], depth["height"], depth["row_bytes"]) == (256, 192, 1024)
        assert (depth["dtype"], depth["byte_order"], depth["units"]) == (
            "float32",
            "little",
            "meters",
        )
        assert depth["sha256"] == _file(phone, "depth_f32").sha256
        assert depth["size"] == _file(phone, "depth_f32").size
        assert _file(phone, "depth_f32").key in depth["url"]
        confidence = found["confidence"]
        assert (confidence["width"], confidence["height"]) == (256, 192)
        assert confidence["row_bytes"] == 256  # none recorded: a row is its width
        assert confidence["dtype"] == "uint8"
        assert confidence["levels"] == {"0": "low", "1": "medium", "2": "high"}
        assert confidence["sha256"] == _file(phone, "confidence_u8").sha256
        mask = found["mask"]
        assert (mask["width"], mask["height"], mask["dtype"]) == (
            MASK_W,
            MASK_H,
            "uint8",
        )
        assert "Fishial" in mask["made_by"]
        assert mask["sha256"] == "e" * 64
        assert "mask_u8" in mask["url"]

    async def test_no_mask_when_the_phone_found_no_fish(
        self, agent, tenants, stores, owner_engine, app_engine  # noqa: F811
    ):
        _, phone = await _phone(
            owner_engine, app_engine, tenants["lab"], stores, mask=False
        )

        found = await agent.call(
            "get_phone_capture_maps",
            slug="lab",
            client_capture_uuid=str(phone.client_capture_uuid),
        )

        assert found["mask"] is None
        assert found["depth"]["url"]

    async def test_another_tenants_capture_is_not_there(
        self, agent, tenants, stores, owner_engine, app_engine  # noqa: F811
    ):
        _, phone = await _phone(
            owner_engine, app_engine, tenants["lab"], stores, mask=False
        )

        error = await agent.error(
            "get_phone_capture_maps",
            slug="partner",
            client_capture_uuid=str(phone.client_capture_uuid),
        )

        assert "404" in error


class TestPhoneMeasurementMask:
    async def test_the_servers_fishial_mask_unvalidated(
        self, agent, tenants, stores, owner_engine, app_engine  # noqa: F811
    ):
        lab = tenants["lab"]
        c, _ = await _phone(owner_engine, app_engine, lab, stores, mask=False)
        result = _fishial_masked(c, stores)
        await _record(app_engine, lab, result)

        found = await agent.call(
            "get_phone_measurement_mask", slug="lab", capture_id=str(c)
        )

        assert found["validated"] is False
        mask = found["result"]
        assert (mask["width"], mask["height"]) == (1920, 1440)
        assert mask["sha256"] == result.mask.sha256
        assert result.mask.key in mask["url"]
        assert "PNG" in mask["format"]
        assert mask["model_id"] == "fishial/1@f721445f37db"

    async def test_a_capture_the_server_never_measured_says_so(
        self, agent, tenants, stores, owner_engine, app_engine  # noqa: F811
    ):
        c, _ = await _phone(
            owner_engine, app_engine, tenants["lab"], stores, mask=False
        )

        error = await agent.error(
            "get_phone_measurement_mask", slug="lab", capture_id=str(c)
        )

        assert "404" in error


class TestAutomaticMask:
    async def test_the_automatic_runs_sam_mask_unvalidated(
        self, agent, tenants, stores, owner_engine, app_engine  # noqa: F811
    ):
        c = await _sam_masked(owner_engine, app_engine, tenants["lab"], stores)

        found = await agent.call("get_automatic_mask", slug="lab", capture_id=str(c))

        assert found["validated"] is False
        mask = found["result"]
        assert (mask["width"], mask["height"]) == (4014, 3016)
        assert mask["sha256"] == "ab" * 32
        assert f"masks/automatic/{c}/m.png" in mask["url"]
        assert "SAM" in mask["made_by"]

    async def test_a_frame_with_no_mask_says_so(
        self, agent, tenants, owner_engine, app_engine
    ):
        from test_automatic_results_store import _measured

        _, c = await _measured(owner_engine, app_engine, tenants["lab"])

        error = await agent.error("get_automatic_mask", slug="lab", capture_id=str(c))

        assert "no mask" in error
