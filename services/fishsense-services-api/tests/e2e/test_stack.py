"""End to end: the built image, as deployed, over real HTTP.

Nothing inside the app is faked. Only Authentik is stood in for, by a static
server publishing a JWKS whose private key the tests hold.
"""

import json
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from fishsense_services_api.migrations import head_revision

pytestmark = pytest.mark.e2e


def test_migrate_reached_head_and_the_tenancy_audit_passed(stack):
    logs = stack.compose("logs", "--no-log-prefix", "migrate")

    assert f"schema at revision {head_revision()}" in logs
    assert "tenancy audit passed" in logs


def test_the_api_is_healthy(stack):
    assert httpx.get(f"{stack.api_url}/healthz").json() == {"status": "ok"}


def test_a_request_without_a_token_is_refused(stack):
    response = httpx.get(f"{stack.api_url}/tenants/any/devices")

    assert response.status_code == 401


def test_a_token_the_identity_provider_did_not_sign_is_refused(stack):
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    response = httpx.get(
        f"{stack.api_url}/tenants/any/devices",
        headers=stack.bearer("sub-mallory", key=forged),
    )

    assert response.status_code == 401


def test_a_member_creates_and_lists_devices(stack):
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    slug = stack.grant(sub)
    url = f"{stack.api_url}/tenants/{slug}/devices"

    created = httpx.post(
        url, json={"kind": "lite", "serial": "TG6-E2E"}, headers=stack.bearer(sub)
    )
    listed = httpx.get(url, headers=stack.bearer(sub))

    assert created.status_code == 201
    assert [d["serial"] for d in listed.json()] == ["TG6-E2E"]


def test_one_tenant_cannot_reach_anothers_devices(stack):
    alice, bob = f"sub-a-{uuid.uuid4().hex[:6]}", f"sub-b-{uuid.uuid4().hex[:6]}"
    alices, bobs = stack.grant(alice), stack.grant(bob)
    httpx.post(
        f"{stack.api_url}/tenants/{bobs}/devices",
        json={"kind": "lite", "serial": "BOB-1"},
        headers=stack.bearer(bob),
    )

    peek = httpx.get(
        f"{stack.api_url}/tenants/{bobs}/devices", headers=stack.bearer(alice)
    )
    own = httpx.get(
        f"{stack.api_url}/tenants/{alices}/devices", headers=stack.bearer(alice)
    )

    assert peek.status_code == 404
    assert own.json() == []


def test_a_first_request_provisions_the_caller(stack):
    sub = f"sub-new-{uuid.uuid4().hex[:8]}"

    httpx.get(f"{stack.api_url}/tenants/any/devices", headers=stack.bearer(sub))

    with stack.owner_engine.connect() as conn:
        found = conn.execute(
            text("SELECT count(*) FROM users WHERE sub = :s"), {"s": sub}
        ).scalar_one()
    assert found == 1


def test_the_api_runs_unprivileged_and_without_owner_credentials(stack):
    uid = stack.compose("exec", "-T", "api", "id", "-u").strip()
    environment = stack.compose("exec", "-T", "api", "env")

    assert uid != "0"
    assert "FISHSENSE_MIGRATION_DATABASE_URL" not in environment
    assert "owner-dev-only" not in environment


def test_an_agent_reads_over_mcp_with_a_read_token(stack):
    """The built image serves /mcp: its lifespan runs the session manager, and
    a read token reaches the tools as its owner."""
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    slug = stack.grant(sub)
    made = httpx.post(
        f"{stack.api_url}/me/tokens", json={"name": "e2e"}, headers=stack.bearer(sub)
    )
    assert made.status_code == 201

    response = httpx.post(
        f"{stack.api_url}/mcp",
        headers={
            "Authorization": f"Bearer {made.json()['token']}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_tenants", "arguments": {}},
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    tenants = json.loads(result["content"][0]["text"])["tenants"]
    assert [t["slug"] for t in tenants] == [slug]
