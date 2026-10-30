"""Missions and deployments over HTTP (PLAN.md §4.9, Phase 2b).

Every route is under ``/tenants/{slug}/`` and needs a member; the writes need
the tenant's admin role. What these pin beyond that:

* a mission shows its facts: purpose, port and what said so, place, trip;
* an operator may state a port only where the owner's rules have not: a
  ``D``-labeled device or the backfill of what was already ingested is fact
  (2026-10-16), and the API will not contradict it;
* deployments are listed, read with their missions, and created by admins.
"""

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator

ISSUER = "https://auth.example.test/application/o/fishsense/"
AUDIENCE = "fishsense-web"
KID = "k1"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ADMIN = "sub-admin"
MEMBER = "sub-member"
OUTSIDER = "sub-outsider"
T0 = datetime(2025, 1, 17, 18, tzinfo=UTC)


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )
    app = create_app(engine=app_engine, validator=validator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


@pytest.fixture
async def tenants(seed_memberships) -> dict:
    return await seed_memberships(
        {
            ADMIN: {"lab": "admin"},
            MEMBER: {"lab": "member"},
            OUTSIDER: {"partner": "admin"},
        }
    )


async def _mission(owner_engine, tenant, number, **columns) -> None:
    columns = {
        "tenant_id": tenant,
        "v1_id": number,
        "name": f"mission {number}",
        "source_path": f"/m/{number}",
        "started_at": T0,
        **columns,
    }
    names = ", ".join(columns)
    values = ", ".join(f":{c}" for c in columns)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO missions ({names}) VALUES ({values})"), columns
        )


async def _deployment(owner_engine, tenant, name) -> int:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO deployments (tenant_id, name, started_on) "
                    "VALUES (:t, :n, '2025-01-17') RETURNING number"
                ),
                {"t": tenant, "n": name},
            )
        ).scalar_one()


async def _join(owner_engine, mission_number, deployment_number) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE missions SET deployment_id = "
                "(SELECT id FROM deployments WHERE number = :d) WHERE number = :m"
            ),
            {"d": deployment_number, "m": mission_number},
        )


# --- reading ------------------------------------------------------------------


async def test_a_mission_shows_its_facts(client, tenants, owner_engine):
    lab = tenants["lab"]
    trip = await _deployment(owner_engine, lab, "2025.01.17.FishSense.San Diego")
    await _mission(
        owner_engine,
        lab,
        480,
        purpose="laser_calibration",
        port="corrective_optic",
        port_source="device_suffix",
        port_note="device labelled FSL-10D",
        country="USA",
        region="San Diego",
        site="Pool",
    )
    await _join(owner_engine, 480, trip)

    listed = await client.get("/tenants/lab/missions", headers=_bearer(MEMBER))
    detail = await client.get("/tenants/lab/missions/480", headers=_bearer(MEMBER))

    assert listed.status_code == 200 and detail.status_code == 200
    [row] = listed.json()
    assert row["purpose"] == "laser_calibration"
    assert (row["port"], row["port_source"]) == ("corrective_optic", "device_suffix")
    assert (row["deployment_number"], row["deployment_name"]) == (
        trip,
        "2025.01.17.FishSense.San Diego",
    )
    body = detail.json()
    assert body["port_note"] == "device labelled FSL-10D"
    assert (body["country"], body["region"], body["site"]) == (
        "USA",
        "San Diego",
        "Pool",
    )
    assert body["source_path"] == "/m/480"
    assert body["captures"] == 0


async def test_a_mission_with_no_facts_says_unknown(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    body = (await client.get("/tenants/lab/missions/7", headers=_bearer(MEMBER))).json()

    assert (body["port"], body["port_source"], body["purpose"]) == (
        "unknown",
        None,
        None,
    )
    assert body["deployment_number"] is None


async def test_another_tenants_mission_is_not_found(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    response = await client.get(
        "/tenants/partner/missions/7", headers=_bearer(OUTSIDER)
    )

    assert response.status_code == 404


# --- the port -------------------------------------------------------------------


async def test_an_admin_states_an_unknown_port(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    response = await client.put(
        "/tenants/lab/missions/7/port",
        json={"port": "flat", "note": "bare flat port, pool test"},
        headers=_bearer(ADMIN),
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["port"], body["port_source"], body["port_note"]) == (
        "flat",
        "operator",
        "bare flat port, pool test",
    )


async def test_a_member_cannot_state_a_port(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    response = await client.put(
        "/tenants/lab/missions/7/port", json={"port": "flat"}, headers=_bearer(MEMBER)
    )

    assert response.status_code == 403


@pytest.mark.parametrize("source", ["device_suffix", "backfill"])
@pytest.mark.parametrize("port", ["flat", "unknown"])
async def test_the_owners_rules_are_not_contradicted(
    client, tenants, owner_engine, source, port
):
    await _mission(
        owner_engine,
        tenants["lab"],
        7,
        port="corrective_optic",
        port_source=source,
    )

    response = await client.put(
        "/tenants/lab/missions/7/port", json={"port": port}, headers=_bearer(ADMIN)
    )

    assert response.status_code == 409
    body = (await client.get("/tenants/lab/missions/7", headers=_bearer(ADMIN))).json()
    assert (body["port"], body["port_source"]) == ("corrective_optic", source)


async def test_restating_what_a_rule_says_changes_nothing(
    client, tenants, owner_engine
):
    await _mission(
        owner_engine,
        tenants["lab"],
        7,
        port="corrective_optic",
        port_source="device_suffix",
    )

    response = await client.put(
        "/tenants/lab/missions/7/port",
        json={"port": "corrective_optic"},
        headers=_bearer(ADMIN),
    )

    assert response.status_code == 200
    assert response.json()["port_source"] == "device_suffix"


async def test_an_operators_port_can_be_withdrawn(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7, port="flat", port_source="operator")

    response = await client.put(
        "/tenants/lab/missions/7/port", json={"port": "unknown"}, headers=_bearer(ADMIN)
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["port"], body["port_source"], body["port_note"]) == (
        "unknown",
        None,
        None,
    )


async def test_an_unrecognized_port_is_refused(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    response = await client.put(
        "/tenants/lab/missions/7/port", json={"port": "pinhole"}, headers=_bearer(ADMIN)
    )

    assert response.status_code == 422


# --- the purpose ----------------------------------------------------------------


async def test_an_admin_states_and_clears_a_purpose(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    stated = await client.put(
        "/tenants/lab/missions/7/purpose",
        json={"purpose": "laser_calibration"},
        headers=_bearer(ADMIN),
    )
    cleared = await client.put(
        "/tenants/lab/missions/7/purpose",
        json={"purpose": None},
        headers=_bearer(ADMIN),
    )
    refused = await client.put(
        "/tenants/lab/missions/7/purpose",
        json={"purpose": "fishing"},
        headers=_bearer(ADMIN),
    )

    assert stated.json()["purpose"] == "laser_calibration"
    assert cleared.json()["purpose"] is None
    assert refused.status_code == 422


# --- deployments ----------------------------------------------------------------


async def test_deployments_are_listed_with_their_mission_counts(
    client, tenants, owner_engine
):
    lab = tenants["lab"]
    trip = await _deployment(owner_engine, lab, "2025.01.17.FishSense.San Diego")
    await _deployment(owner_engine, tenants["partner"], "partner's trip")
    for number in (480, 481):
        await _mission(owner_engine, lab, number)
        await _join(owner_engine, number, trip)

    response = await client.get("/tenants/lab/deployments", headers=_bearer(MEMBER))

    assert response.status_code == 200
    [row] = response.json()
    assert (row["number"], row["name"], row["missions"]) == (
        trip,
        "2025.01.17.FishSense.San Diego",
        2,
    )
    assert row["started_on"] == "2025-01-17"


async def test_a_deployment_is_read_with_its_missions(client, tenants, owner_engine):
    lab = tenants["lab"]
    trip = await _deployment(owner_engine, lab, "2025.01.17.FishSense.San Diego")
    await _mission(owner_engine, lab, 480)
    await _mission(owner_engine, lab, 9)
    await _join(owner_engine, 480, trip)

    response = await client.get(
        f"/tenants/lab/deployments/{trip}", headers=_bearer(MEMBER)
    )
    missing = await client.get(
        "/tenants/lab/deployments/999999", headers=_bearer(MEMBER)
    )

    assert response.status_code == 200
    assert [m["number"] for m in response.json()["missions"]] == [480]
    assert missing.status_code == 404


async def test_an_admin_creates_a_deployment_once(client, tenants, owner_engine):
    body = {
        "name": "2026.03.02.FishSense.Curacao",
        "started_on": "2026-03-02",
        "ended_on": "2026-03-09",
        "notes": "survey trip",
    }

    created = await client.post(
        "/tenants/lab/deployments", json=body, headers=_bearer(ADMIN)
    )
    again = await client.post(
        "/tenants/lab/deployments", json=body, headers=_bearer(ADMIN)
    )
    by_member = await client.post(
        "/tenants/lab/deployments",
        json={**body, "name": "another"},
        headers=_bearer(MEMBER),
    )
    backwards = await client.post(
        "/tenants/lab/deployments",
        json={**body, "name": "backwards", "ended_on": "2026-03-01"},
        headers=_bearer(ADMIN),
    )

    assert created.status_code == 201
    assert created.json()["name"] == body["name"]
    assert again.status_code == 409
    assert by_member.status_code == 403
    assert backwards.status_code == 422


async def test_an_admin_moves_a_mission_into_and_out_of_a_deployment(
    client, tenants, owner_engine
):
    lab = tenants["lab"]
    trip = await _deployment(owner_engine, lab, "trip")
    partners = await _deployment(owner_engine, tenants["partner"], "partner trip")
    await _mission(owner_engine, lab, 7)

    joined = await client.put(
        f"/tenants/lab/missions/7/deployment/{trip}", headers=_bearer(ADMIN)
    )
    foreign = await client.put(
        f"/tenants/lab/missions/7/deployment/{partners}", headers=_bearer(ADMIN)
    )
    left = await client.delete(
        "/tenants/lab/missions/7/deployment", headers=_bearer(ADMIN)
    )

    assert joined.status_code == 200
    assert joined.json()["deployment_number"] == trip
    assert foreign.status_code == 404
    assert left.status_code == 204
    body = (await client.get("/tenants/lab/missions/7", headers=_bearer(ADMIN))).json()
    assert body["deployment_number"] is None


async def test_slate_is_not_a_purpose(client, tenants, owner_engine):
    await _mission(owner_engine, tenants["lab"], 7)

    response = await client.put(
        "/tenants/lab/missions/7/purpose",
        json={"purpose": "slate"},
        headers=_bearer(ADMIN),
    )

    assert response.status_code == 422


async def _pinhole_device(owner_engine, tenant):
    async with owner_engine.begin() as conn:
        device = (
            await conn.execute(
                text(
                    "INSERT INTO devices (tenant_id, kind, serial) "
                    "VALUES (:t, 'lite', :s) RETURNING id"
                ),
                {"t": tenant, "s": f"S{tenant.hex[:8]}"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
                "camera_matrix, distortion_coefficients) VALUES (:t, :d, 'pinhole', "
                "'[[1,0,0],[0,1,0],[0,0,1]]', '[0,0,0,0,0]')"
            ),
            {"t": tenant, "d": device},
        )
    return device


@pytest.mark.parametrize(
    "port, source, device, processable",
    [
        ("corrective_optic", "backfill", True, True),
        ("flat", "operator", True, False),
        ("unknown", None, True, False),
        ("corrective_optic", "backfill", False, False),
    ],
)
async def test_a_mission_says_whether_the_pipeline_will_process_it(
    client, tenants, owner_engine, port, source, device, processable
):
    """The stages' own predicates (camera_sql), computed by the API, so the
    page never restates the rule (2b-2): the corrective optic and a pinhole
    camera calibration."""
    lab = tenants["lab"]
    await _mission(
        owner_engine,
        lab,
        7,
        port=port,
        port_source=source,
        device_id=await _pinhole_device(owner_engine, lab) if device else None,
    )

    listed = (await client.get("/tenants/lab/missions", headers=_bearer(MEMBER))).json()
    detail = (
        await client.get("/tenants/lab/missions/7", headers=_bearer(MEMBER))
    ).json()

    assert listed[0]["processable"] is processable
    assert detail["processable"] is processable


async def test_a_borrowed_calibration_from_another_port_is_not_processable(
    client, tenants, owner_engine
):
    lab = tenants["lab"]
    device = await _pinhole_device(owner_engine, lab)
    await _mission(owner_engine, lab, 6, port="flat", port_source="operator")
    async with owner_engine.connect() as conn:
        source = (
            await conn.execute(text("SELECT id FROM missions WHERE number = 6"))
        ).scalar_one()
    await _mission(
        owner_engine,
        lab,
        7,
        port="corrective_optic",
        port_source="backfill",
        device_id=device,
        calibration_source_mission_id=source,
    )

    detail = (
        await client.get("/tenants/lab/missions/7", headers=_bearer(MEMBER))
    ).json()

    assert detail["processable"] is False
