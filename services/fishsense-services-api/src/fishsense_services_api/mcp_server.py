"""The MCP endpoint at /mcp: lets agents read FishSense (the owner, 2026-10-23).

Ported in shape from e4e-reference-manager's ``app/mcp_server.py``: streamable
HTTP, stateless, JSON responses, and each tool a thin wrapper over a REST
endpoint, called in-process with the caller's own ``Authorization`` header. So
a tool sees exactly what the API shows that user -- their tenants, or for a
system admin every tenant read-only, each visit recorded (0056) -- and nothing
here re-implements a permission or touches the database itself.

v2 changes from the reference manager:

* **Read-only, always.** The reference manager's tools add, edit and trash;
  these only read. The endpoint takes a personal **read** token alone (0066):
  an edit token is refused (it would gain nothing here, and would put write
  power in an agent's config), and so is an Authentik session. Writes are for
  the REST API, with an edit token the owner opted into.
* **The guard is the transport.** Tools reach the API through
  :class:`ReadOnlyTransport`, which sends GETs and nothing else, so even a
  tool written wrongly cannot write -- and a read token is refused every write
  by the API besides.
* **Automatic results say so.** An automatic result comes back wrapped as
  ``validated: false``, so a model can't quote it as a measurement.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from fishsense_services_api.api_tokens import READ, TOKEN_PREFIX, token_principal

INSTRUCTIONS = (
    "Read-only tools over FishSense: fish measured from underwater photos, "
    "grouped into missions (dives) inside tenants (a lab or a partner). Start "
    "with list_tenants; every other tool takes a tenant's `slug`. Missions and "
    "deployments are numbered within their tenant. A system admin sees every "
    "tenant, read-only, and each tenant they open is recorded. Automatic results "
    "(get_mission_automatic, get_capture_automatic) come back with "
    "`validated: false`: they have not been reviewed by a person, so say so "
    "wherever you use one and never present it as a measurement. When you report "
    "numbers, give how many fish or frames they rest on. Nothing here changes "
    "data; changes are made in the web app."
)
UNVALIDATED = (
    "An automatic result, not reviewed by a person: say so wherever it is used, "
    "and don't present it as a measurement."
)

_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

Slug = Annotated[str, Field(min_length=1, description="The tenant's slug")]
Number = Annotated[int, Field(ge=0, description="The mission's number in its tenant")]


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    """Sends GETs, and refuses anything else before it leaves."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise PermissionError(f"the MCP tools only read, not {request.method}")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _path(*segments: object) -> str:
    return "/" + "/".join(quote(str(s), safe="") for s in segments)


def _error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return f"{response.status_code}: {detail or response.reason_phrase}"


def _automatic(result: Any) -> dict:
    return {"validated": False, "note": UNVALIDATED, "result": result}


class McpEndpoint:
    """ASGI endpoint for /mcp: requires a live read token, then hands the
    request to the SDK's session manager. A manager runs only once, so
    :meth:`lifespan` builds a fresh one each time the app starts."""

    def __init__(self, app: FastAPI, engine: AsyncEngine) -> None:
        self._app, self._engine = app, engine
        self._manager: Any = None
        self.server = _server(self._get)

    @asynccontextmanager
    async def lifespan(self, _: FastAPI) -> AsyncIterator[None]:
        self.server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            # DNS-rebinding protection guards unauthenticated localhost
            # servers; this one is public and every request carries a token.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        manager = self.server.session_manager
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refusal = await self._refusal(Headers(scope=scope).get("authorization", ""))
        if refusal is not None:
            await refusal(scope, receive, send)
        elif self._manager is None:
            await JSONResponse({"detail": "MCP is not running"}, status_code=503)(
                scope, receive, send
            )
        else:
            await self._manager.handle_request(scope, receive, send)

    async def _refusal(self, authorization: str) -> JSONResponse | None:
        scheme, _, raw = authorization.partition(" ")
        raw = raw.strip()
        if scheme.lower() != "bearer" or not raw.startswith(TOKEN_PREFIX):
            return _unauthorized(
                "a personal API token is required (Bearer fishsense_…)"
            )
        owner = await token_principal(self._engine, raw)
        if owner is None:
            return _unauthorized("invalid, expired or revoked token")
        if owner.scope != READ:
            return JSONResponse(
                {"detail": "MCP takes a read-only token: make one in the web app"},
                status_code=403,
            )
        return None

    async def _get(
        self, ctx: Context, path: str, *, params: dict | None = None, missing=False
    ) -> Any:
        """GET ``path`` from the API as the caller. ``missing``: return None
        on a 404 rather than raising it."""
        authorization = (ctx.headers or {}).get("authorization", "")
        # raise_app_exceptions=False: a crash inside an endpoint comes back as
        # its 500, reported to the agent as a tool error.
        inner = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=ReadOnlyTransport(inner),
            base_url="http://fishsense.internal",
            headers={"Authorization": authorization},
        ) as api:
            query = {k: v for k, v in (params or {}).items() if v is not None}
            response = await api.get(path, params=query)
        if missing and response.status_code == 404:
            return None
        if response.is_error:
            raise ToolError(_error(response))
        return response.json()


def _server(get) -> MCPServer:
    mcp = MCPServer(name="FishSense", instructions=INSTRUCTIONS)
    tool = mcp.tool(annotations=_READ)

    @tool
    async def list_tenants(ctx: Context) -> dict:
        """The tenants you can read: yours as a member (with your role), and,
        if you are a system admin, every other one read-only."""
        mine = await get(ctx, "/me/memberships")
        tenants = [
            {
                "slug": t["slug"],
                "name": t["name"],
                "access": "member",
                "role": t["role"],
            }
            for t in mine
        ]
        theirs = {t["slug"] for t in mine}
        for t in await get(ctx, "/admin/tenants", missing=True) or []:
            if t["slug"] not in theirs:
                tenants.append(
                    {
                        "slug": t["slug"],
                        "name": t["name"],
                        "access": "system_admin_read_only",
                        "role": None,
                    }
                )
        return {"tenants": sorted(tenants, key=lambda t: t["slug"])}

    @tool
    async def get_tenant_summary(ctx: Context, slug: Slug) -> dict:
        """A tenant's numbers: missions, frames, wild fish, lengths, species."""
        return await get(ctx, _path("tenants", slug, "summary"))

    @tool
    async def get_tenant_regions(ctx: Context, slug: Slug) -> dict:
        """Where a tenant's wild fish were measured, by broad region."""
        return await get(ctx, _path("tenants", slug, "regions"))

    @tool
    async def list_missions(ctx: Context, slug: Slug) -> dict:
        """A tenant's missions (dives), with their device, port, calibration
        and capture counts."""
        return {"missions": await get(ctx, _path("tenants", slug, "missions"))}

    @tool
    async def get_mission(ctx: Context, slug: Slug, number: Number) -> dict:
        """One mission in full."""
        return await get(ctx, _path("tenants", slug, "missions", number))

    @tool
    async def list_mission_captures(
        ctx: Context,
        slug: Slug,
        number: Number,
        limit: Annotated[int, Field(ge=1, le=200)] = 48,
        after: Annotated[
            str | None, Field(description="The last page's `next`, to page on")
        ] = None,
    ) -> dict:
        """A mission's captures, measured or not, in the order taken; a page
        at a time."""
        return await get(
            ctx,
            _path("tenants", slug, "missions", number, "captures"),
            params={"limit": limit, "after": after},
        )

    @tool
    async def get_mission_automatic(ctx: Context, slug: Slug, number: Number) -> dict:
        """The automatic pipeline's results for a mission: statuses, lengths,
        refusals and, where human labels exist, how far it is from them.
        Unvalidated."""
        return _automatic(
            await get(ctx, _path("tenants", slug, "missions", number, "automatic"))
        )

    @tool
    async def get_capture_automatic(
        ctx: Context, slug: Slug, capture_id: uuid.UUID
    ) -> dict:
        """The automatic pipeline's result for one capture. Unvalidated."""
        return _automatic(
            await get(ctx, _path("tenants", slug, "captures", capture_id, "automatic"))
        )

    @tool
    async def list_capture_reviews(
        ctx: Context, slug: Slug, capture_id: uuid.UUID
    ) -> dict:
        """People's reviews of a capture's automatic result: accepted, fixed or
        rejected."""
        return {
            "reviews": await get(
                ctx, _path("tenants", slug, "captures", capture_id, "reviews")
            )
        }

    @tool
    async def list_observations(
        ctx: Context,
        slug: Slug,
        device: Literal["lite", "mobile", "other"] | None = None,
        mission: Annotated[int | None, Field(ge=0)] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        page: Annotated[int | None, Field(ge=1)] = None,
        before: Annotated[
            str | None, Field(description="The last page's `next`, to page on")
        ] = None,
    ) -> dict:
        """Measured fish, newest first, a page at a time (by `page` or after a
        cursor, not both); `total` is how many there are in all."""
        return await get(
            ctx,
            _path("tenants", slug, "observations"),
            params={
                "device": device,
                "mission": mission,
                "limit": limit,
                "page": page,
                "before": before,
            },
        )

    @tool
    async def get_observation(
        ctx: Context, slug: Slug, capture_id: uuid.UUID, fish_id: uuid.UUID
    ) -> dict:
        """One measured fish in full: how it was measured, and with what."""
        return await get(
            ctx, _path("tenants", slug, "observations", capture_id, fish_id)
        )

    @tool
    async def list_deployments(ctx: Context, slug: Slug) -> dict:
        """A tenant's deployments: trips that group missions."""
        return {"deployments": await get(ctx, _path("tenants", slug, "deployments"))}

    @tool
    async def get_deployment(
        ctx: Context, slug: Slug, number: Annotated[int, Field(ge=1)]
    ) -> dict:
        """One deployment, with its missions."""
        return await get(ctx, _path("tenants", slug, "deployments", number))

    @tool
    async def list_devices(ctx: Context, slug: Slug) -> dict:
        """A tenant's devices (cameras), by kind and serial."""
        return {"devices": await get(ctx, _path("tenants", slug, "devices"))}

    return mcp


def mount_mcp(app: FastAPI, engine: AsyncEngine) -> McpEndpoint:
    """Serve /mcp on ``app``, its session manager running with the app."""
    endpoint = McpEndpoint(app, engine)
    app.router.routes.append(
        Route("/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"])
    )
    app.router.lifespan_context = endpoint.lifespan
    return endpoint


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )
