"""The MCP endpoint at /mcp (the owner, 2026-10-23).

Agents connect with a personal **read** token; the endpoint refuses edit tokens
and sessions, and every tool reads. Each tool calls a REST endpoint in-process
with the caller's own token, so what an agent sees is exactly what the API
shows that user: their tenants, or, for a system admin, every tenant read-only,
each visit recorded (0056).

Driven with the MCP SDK's own client over an in-process transport, so this
exercises the real protocol, the token check and the REST permissions.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from depth_measure_seed import capture, exec_, mission
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.mcp_server import (
    InProcessTransport,
    ReadOnlyTransport,
    mount_mcp,
)

ISSUER, AUDIENCE, KID = "https://idp.test", "fishsense-api", "test-key"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MEMBER, OPERATOR = "member-sub", "operator-sub"

EXPECTED_TOOLS = {
    "list_tenants",
    "get_tenant_summary",
    "get_tenant_regions",
    "list_missions",
    "get_mission",
    "list_mission_captures",
    "get_mission_automatic",
    "get_capture_automatic",
    "list_automatic_lengths",
    "list_mission_automatic_lengths",
    "list_capture_reviews",
    "list_observations",
    "get_observation",
    "list_deployments",
    "get_deployment",
    "list_devices",
}


def _signed_in(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app(app_engine):
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: KEY.public_key()}),
    )
    return create_app(engine=app_engine, validator=validator)


@pytest.fixture
async def tenants(seed_memberships, owner_engine):
    found = await seed_memberships(
        {MEMBER: {"lab": "admin"}, OPERATOR: {"lab": "member"}}
    )
    found |= await seed_memberships({"someone": {"partner": "admin"}})
    await exec_(
        owner_engine,
        "UPDATE users SET is_system_admin = true WHERE sub = :s",
        s=OPERATOR,
    )
    return found


async def _token(app, sub: str, scope: str = "read") -> str:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        made = await c.post(
            "/me/tokens",
            headers=_signed_in(sub),
            json={"name": "agent", "scope": scope},
        )
    assert made.status_code == 201, made.text
    return made.json()["token"]


@asynccontextmanager
async def _session(app, authorization: str):
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://api.test",
            headers={"Authorization": authorization},
        ) as http,
        streamable_http_client("http://api.test/mcp", http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


class Agent:
    """An MCP client holding a token. The server is stateless, so each call
    opens its own session, keeping anyio's task groups inside one task."""

    def __init__(self, app, token: str):
        self.app, self.authorization = app, f"Bearer {token}"

    async def list_tools(self):
        async with _session(self.app, self.authorization) as s:
            return (await s.list_tools()).tools

    async def call(self, tool: str, **args):
        async with _session(self.app, self.authorization) as s:
            result = await s.call_tool(tool, args)
        assert not result.is_error, result.content
        return json.loads(result.content[0].text)

    async def error(self, tool: str, **args) -> str:
        async with _session(self.app, self.authorization) as s:
            result = await s.call_tool(tool, args)
        assert result.is_error
        return result.content[0].text


@pytest.fixture
async def agent(app, tenants) -> Agent:
    return Agent(app, await _token(app, MEMBER))


@pytest.fixture
async def operator(app, tenants) -> Agent:
    return Agent(app, await _token(app, OPERATOR))


async def _post_mcp(app, headers: dict[str, str]) -> httpx2.Response:
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
        ) as http,
    ):
        return await http.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )


# ── Who may connect ──────────────────────────────────────────────────────────────────


async def test_lists_its_tools(agent):
    assert {t.name for t in await agent.list_tools()} == EXPECTED_TOOLS


async def test_every_tool_says_it_only_reads(agent):
    for tool in await agent.list_tools():
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


async def test_it_needs_a_token(app, tenants):
    response = await _post_mcp(app, {})

    assert response.status_code == 401


async def test_a_signed_in_session_is_not_a_token(app, tenants):
    response = await _post_mcp(app, _signed_in(MEMBER))

    assert response.status_code == 401
    assert "personal API token is required" in response.json()["detail"]


async def test_it_refuses_an_edit_token(app, tenants):
    token = await _token(app, MEMBER, scope="edit")

    response = await _post_mcp(app, {"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert "read-only token" in response.json()["detail"]


async def test_it_refuses_a_revoked_token(app, tenants, owner_engine):
    token = await _token(app, MEMBER)
    await exec_(owner_engine, "UPDATE api_tokens SET revoked_at = now()")

    response = await _post_mcp(app, {"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


# ── What the tools see ───────────────────────────────────────────────────────────────


async def test_a_member_lists_their_own_tenants(agent):
    listed = await agent.call("list_tenants")

    assert listed == {
        "tenants": [{"slug": "lab", "name": "lab", "access": "member", "role": "admin"}]
    }


async def test_a_system_admin_lists_every_tenant_and_how_they_see_it(operator):
    listed = await operator.call("list_tenants")

    assert listed == {
        "tenants": [
            {"slug": "lab", "name": "lab", "access": "member", "role": "member"},
            {
                "slug": "partner",
                "name": "partner",
                "access": "system_admin_read_only",
                "role": None,
            },
        ]
    }


async def test_a_tool_reads_what_the_api_shows(app, agent):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        rest = (await c.get("/tenants/lab/summary", headers=_signed_in(MEMBER))).json()

    assert await agent.call("get_tenant_summary", slug="lab") == rest


async def test_another_tenant_is_not_there(agent):
    assert "404" in await agent.error("get_tenant_summary", slug="partner")


async def test_a_system_admin_reads_any_tenant_and_the_visit_is_recorded(
    operator, owner_engine
):
    await operator.call("get_tenant_summary", slug="partner")

    async with owner_engine.connect() as conn:
        actions = await conn.execute(text("""
            SELECT t.slug, a.action FROM system_admin_access a
            JOIN tenants t ON t.id = a.tenant_id
            """))
        assert [tuple(r) for r in actions] == [("partner", "view")]


async def test_missions_and_their_captures(agent, tenants, owner_engine):
    dive = await mission(owner_engine, tenants["lab"])
    await capture(owner_engine, tenants["lab"], dive)

    missions = await agent.call("list_missions", slug="lab")
    number = missions["missions"][0]["number"]
    detail = await agent.call("get_mission", slug="lab", number=number)
    captures = await agent.call("list_mission_captures", slug="lab", number=number)

    assert len(missions["missions"]) == 1
    assert detail["number"] == number
    assert len(captures["captures"]) == 1


async def test_automatic_results_are_marked_unvalidated(agent, tenants, owner_engine):
    await mission(owner_engine, tenants["lab"])
    number = (await agent.call("list_missions", slug="lab"))["missions"][0]["number"]

    result = await agent.call("get_mission_automatic", slug="lab", number=number)

    assert result["validated"] is False
    assert "not reviewed" in result["note"]
    assert "result" in result


async def test_automatic_lengths_come_unvalidated_with_their_reviews(
    agent, tenants, owner_engine, app_engine
):
    from test_automatic_results_store import _measured

    mission_id, c = await _measured(owner_engine, app_engine, tenants["lab"])
    async with owner_engine.connect() as conn:
        number = (
            await conn.execute(
                text("SELECT number FROM missions WHERE id = :m"), {"m": mission_id}
            )
        ).scalar_one()

    tenant_wide = await agent.call("list_automatic_lengths", slug="lab")
    one_mission = await agent.call(
        "list_mission_automatic_lengths", slug="lab", number=number
    )

    for result in (tenant_wide, one_mission):
        assert result["validated"] is False
        assert "not reviewed" in result["note"]
        assert [r["capture_id"] for r in result["lengths"]] == [str(c)]
        assert result["lengths"][0]["reviews"] == {}
        assert result["next"] is None


async def test_an_inner_failure_is_a_tool_error(agent):
    assert "404" in await agent.error("get_mission", slug="lab", number=999)


# ── The guard ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_the_tools_client_cannot_write(method):
    """Every tool calls the API through this transport: it sends GETs and
    nothing else, whatever a tool asks."""

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=ReadOnlyTransport(Inner()), base_url="http://x"
    ) as client:
        with pytest.raises(PermissionError):
            await client.request(method, "/tenants/lab/deployments")
        assert (await client.get("/tenants/lab/summary")).status_code == 200


async def test_the_api_runs_in_process_without_waiting_for_background_work():
    """A route's background work (the Fish list names places after it
    answers) must not hold up the tool: httpx's own ASGITransport waits for it."""
    import asyncio

    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse
    from starlette.routing import Route as StarletteRoute

    never = asyncio.Event()

    async def page(_):
        return JSONResponse({"ok": True}, background=BackgroundTask(never.wait))

    app = Starlette(routes=[StarletteRoute("/", page)])
    async with httpx.AsyncClient(
        transport=InProcessTransport(app), base_url="http://x"
    ) as client:
        response = await asyncio.wait_for(client.get("/"), timeout=2)

    assert response.json() == {"ok": True}
    never.set()


async def test_the_api_in_process_reports_a_crash_as_a_500():
    from starlette.applications import Starlette
    from starlette.routing import Route as StarletteRoute

    async def broken(_):
        raise RuntimeError("boom")

    app = Starlette(routes=[StarletteRoute("/", broken)])
    async with httpx.AsyncClient(
        transport=InProcessTransport(app), base_url="http://x"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 500


async def test_a_tool_call_looks_the_token_up_once(app, tenants, monkeypatch):
    """The endpoint resolves the token; the tool's in-process reads (two, for
    list_tenants) act as that owner rather than resolving it again each."""
    import fishsense_services_api.api_tokens as api_tokens
    import fishsense_services_api.app as app_module

    token = await _token(app, OPERATOR)
    calls = []
    real = api_tokens.token_principal

    async def counted(engine, raw):
        calls.append(raw)
        return await real(engine, raw)

    monkeypatch.setattr(api_tokens, "token_principal", counted)
    monkeypatch.setattr(app_module, "token_principal", counted)

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
        ) as http,
    ):
        response = await http.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_tenants", "arguments": {}},
            },
        )

    assert not response.json()["result"].get("isError"), response.json()
    assert len(calls) == 1


async def test_mounting_keeps_the_apps_own_lifespan(app_engine):
    from fastapi import FastAPI

    ran = []

    @asynccontextmanager
    async def own(_):
        ran.append("start")
        yield
        ran.append("stop")

    app = FastAPI(lifespan=own)
    endpoint = mount_mcp(app, app_engine)

    async with app.router.lifespan_context(app):
        assert endpoint.running
    assert ran == ["start", "stop"]
