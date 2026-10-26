"""The MCP endpoint at /mcp: lets agents read FishSense (the owner, 2026-10-23).

Ported in shape from e4e-reference-manager@a12bcca ``api/app/mcp_server.py``:
streamable HTTP, stateless, JSON responses, and each tool a thin wrapper over a
REST endpoint, called in-process as the caller. So a tool sees exactly what the
API shows that user -- their tenants, or for a system admin every tenant
read-only, each visit recorded (0056) -- and nothing here re-implements a
permission or touches the database itself.

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
* **The token is looked up once a call.** The endpoint resolves it, and hands
  its owner to the routes it calls through the ASGI scope
  (``IN_PROCESS_PRINCIPAL``); the reference manager forwards the header, and
  every in-process GET looked the token up again (the PR #335 review).
* **Background work doesn't hold a tool up.** httpx's ``ASGITransport`` waits
  for the whole app call, background tasks included, so a tool reading the
  Fish list waited on the geocoder. :class:`InProcessTransport` answers once
  the response is sent, and leaves the background work running.
"""

import asyncio
import logging
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

from fishsense_services_api import api_tokens
from fishsense_services_api.api_tokens import READ, TOKEN_PREFIX
from fishsense_services_api.auth import IN_PROCESS_PRINCIPAL, Principal

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Read-only tools over FishSense: fish measured from underwater photos, "
    "grouped into missions (dives) inside tenants (a lab or a partner). Start "
    "with list_tenants; every other tool takes a tenant's `slug`. Missions and "
    "deployments are numbered within their tenant. A system admin sees every "
    "tenant, read-only, and each tenant they open is recorded. Automatic results "
    "(get_mission_automatic, get_capture_automatic, list_automatic_lengths, "
    "list_mission_automatic_lengths) come back with `validated: false`: they "
    "have not been reviewed by a person, so say so wherever you use one and never "
    "present it as a measurement. Each automatic length carries its reviews: a "
    "laser or head_tail review whose action is accept or fix has become a human "
    "label, and `human_length_m` is the frame's human length when there is one; "
    "prefer it. Lengths come a page at a time: pass `next` as `after` until it "
    "is null (a page may be short and still have a next). A mission's "
    "`laser_colors` counts its frames by laser colour from three sources "
    "(labellers, the human path's predictions, the automatic dot): say which "
    "you use, and say so when they disagree. An automatic dot's "
    "`laser_color_source` is `dot` (read with the dot) or `render` (read off "
    "its render later, by a backfill). When you report "
    "numbers, give how many fish or frames they rest on. These tools only "
    "read; changes are made in the web app."
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


class InProcessTransport(httpx.AsyncBaseTransport):
    """Calls an ASGI app in this process, answering as soon as the response is
    sent. Its background work carries on, as it would behind a server; a crash
    before any response is a 500, as a server would send. The caller a request
    acts as rides in its ``IN_PROCESS_PRINCIPAL`` extension, into the scope."""

    def __init__(self, app) -> None:
        self._app = app
        self._running: set[asyncio.Task] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        url = request.url
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": url.scheme,
            "path": url.path,
            "raw_path": url.raw_path.split(b"?")[0],
            "query_string": url.query,
            "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "client": ("127.0.0.1", 0),
            "server": (url.host, url.port or 80),
            "extensions": {},
        }
        principal = request.extensions.get(IN_PROCESS_PRINCIPAL)
        if principal is not None:
            scope[IN_PROCESS_PRINCIPAL] = principal
        answered = asyncio.Event()
        started: dict = {}
        chunks: list[bytes] = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await answered.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                started.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    answered.set()

        task = asyncio.create_task(self._app(scope, receive, send))
        self._running.add(task)
        task.add_done_callback(self._finished)
        waiting = asyncio.ensure_future(answered.wait())
        try:
            await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
        if not answered.is_set():
            return httpx.Response(500, request=request)
        return httpx.Response(
            started.get("status", 500),
            headers=started.get("headers", []),
            content=b"".join(chunks),
            request=request,
        )

    def _finished(self, task: asyncio.Task) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("in-process API call failed", exc_info=task.exception())


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


def _automatic_page(page: dict) -> dict:
    """A page of automatic lengths, marked as `_automatic` marks a result."""
    return {"validated": False, "note": UNVALIDATED, **page}


class McpEndpoint:
    """ASGI endpoint for /mcp: requires a live read token, then hands the
    request to the SDK's session manager. A manager runs only once, so
    :meth:`lifespan` builds a fresh one each time the app starts."""

    def __init__(self, app: FastAPI, engine: AsyncEngine) -> None:
        self._engine = engine
        self._transport = ReadOnlyTransport(InProcessTransport(app))
        self._api: httpx.AsyncClient | None = None
        self._manager: Any = None
        self.server = _server(self._get)

    @property
    def running(self) -> bool:
        return self._manager is not None

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
        async with (
            httpx.AsyncClient(
                transport=self._transport, base_url="http://fishsense.internal"
            ) as api,
            manager.run(),
        ):
            self._api, self._manager = api, manager
            try:
                yield
            finally:
                self._api, self._manager = None, None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        caller = await self._caller(Headers(scope=scope).get("authorization", ""))
        if isinstance(caller, JSONResponse):
            await caller(scope, receive, send)
        elif self._manager is None:
            await JSONResponse({"detail": "MCP is not running"}, status_code=503)(
                scope, receive, send
            )
        else:
            # The tools act as this caller (see `_get`): never set from outside.
            await self._manager.handle_request(
                {**scope, IN_PROCESS_PRINCIPAL: caller}, receive, send
            )

    async def _caller(self, authorization: str) -> Principal | JSONResponse:
        """The read token's owner, looked up once a call; else the refusal."""
        scheme, _, raw = authorization.partition(" ")
        raw = raw.strip()
        if scheme.lower() != "bearer" or not raw.startswith(TOKEN_PREFIX):
            return _unauthorized(
                "a personal API token is required (Bearer fishsense_…)"
            )
        owner = await api_tokens.token_principal(self._engine, raw)
        if owner is None:
            return _unauthorized("invalid, expired or revoked token")
        if owner.scope != READ:
            return JSONResponse(
                {"detail": "MCP takes a read-only token: make one in the web app"},
                status_code=403,
            )
        return Principal(sub=owner.sub, token_scope=owner.scope)

    async def _get(
        self, ctx: Context, path: str, *, params: dict | None = None, missing=False
    ) -> Any:
        """GET ``path`` from the API as the caller. ``missing``: return None
        on a 404 rather than raising it."""
        caller = ctx.request_context.request.scope.get(IN_PROCESS_PRINCIPAL)
        if caller is None or self._api is None:
            raise ToolError("no caller: MCP is not running")
        query = {k: v for k, v in (params or {}).items() if v is not None}
        response = await self._api.get(
            path, params=query, extensions={IN_PROCESS_PRINCIPAL: caller}
        )
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
    async def list_mission_laser_colors(ctx: Context, slug: Slug) -> dict:
        """Every mission's frames by laser colour, in one call: for each mission
        with a dot, counts of red, green and unread from the labellers, the
        human path's laser stage and the automatic dot -- as get_mission's
        `laser_colors`, for the whole tenant."""
        return {"missions": await get(ctx, _path("tenants", slug, "laser-colors"))}

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
    async def list_automatic_lengths(
        ctx: Context,
        slug: Slug,
        species: Annotated[
            str | None,
            Field(description="A scientific name, e.g. Lachnolaimus maximus"),
        ] = None,
        after: Annotated[
            str | None, Field(description="The last page's `next`, to page on")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict:
        """A tenant's automatic lengths, by mission and capture, a page at a
        time: each with its species, the frame's human length if any, and its
        reviews. Slate frames are left out. Unvalidated."""
        page = await get(
            ctx,
            _path("tenants", slug, "automatic", "lengths"),
            params={"species": species, "after": after, "limit": limit},
        )
        return _automatic_page(page)

    @tool
    async def list_mission_automatic_lengths(
        ctx: Context,
        slug: Slug,
        number: Number,
        after: Annotated[
            str | None, Field(description="The last page's `next`, to page on")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict:
        """One mission's automatic lengths, by capture, a page at a time, as
        list_automatic_lengths gives them. Unvalidated."""
        page = await get(
            ctx,
            _path("tenants", slug, "missions", number, "automatic", "lengths"),
            params={"after": after, "limit": limit},
        )
        return _automatic_page(page)

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
    """Serve /mcp on ``app``, its session manager running inside whatever
    lifespan the app already has."""
    endpoint = McpEndpoint(app, engine)
    app.router.routes.append(
        Route("/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"])
    )
    own = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(running: FastAPI):
        async with own(running) as state, endpoint.lifespan(running):
            yield state

    app.router.lifespan_context = lifespan
    return endpoint


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
    )
