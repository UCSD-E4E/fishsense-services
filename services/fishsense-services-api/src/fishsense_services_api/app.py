"""The HTTP API.

Each request goes through the same four steps before touching tenant data:

1. authenticate the bearer token in-app (401; 503 if the IdP's keys can't be
   fetched -- an outage is not a bad token). It is an Authentik session, or a
   personal API token (0066) acting as its owner: a ``read`` token is refused
   every write request here, for every route at once (a GET keeps its own
   server-side effects, such as recording a system admin's visit);
2. provision the caller's user row on first sight, and -- for a partner whose
   token carries an ``org`` claim -- their membership in the tenant that
   claims that org (as ``member``; migration 0037);
3. resolve their membership in the tenant named by the path (404 for both
   "not a member" and "no such tenant", so tenants can't be probed);
4. run the tenant work in a tenant-scoped transaction, under RLS.
"""

import math
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.api_tokens import (
    DEFAULT_DAYS,
    EDIT,
    MAX_DAYS,
    TOKEN_PREFIX,
    Scope,
    create_token,
    my_tokens,
    revoke_token,
    token_principal,
)
from fishsense_services_api.auth import (
    IN_PROCESS_PRINCIPAL,
    InvalidToken,
    KeysUnavailable,
    Principal,
    Validator,
)
from fishsense_services_api.db import principal_transaction, tenant_transaction
from fishsense_services_api.memberships import (
    UNLOCKED_ACCESS,
    Membership,
    join_claimed_tenant,
    list_memberships,
    resolve_membership,
)
from fishsense_services_api.automatic_api import add_automatic_routes
from fishsense_services_api.review_api import add_review_routes
from fishsense_services_api.mcp_server import mount_mcp
from fishsense_services_api.mobile_api import add_mobile_routes
from fishsense_services_api.place_names import Geocoder
from fishsense_services_api.missions_api import add_mission_routes
from fishsense_services_api.observations_api import add_observation_routes
from fishsense_services_api.place_naming import PlaceNamer
from fishsense_services_api.public_api import add_public_routes
from fishsense_services_api.portal_api import add_portal_routes
from fishsense_services_api.tenant_stores import TenantStores
from fishsense_services_api.users import provision_user, record_profile

UNIQUE_VIOLATION = "23505"  # Postgres SQLSTATE


class Me(BaseModel):
    #: May open any tenant (0056): read-only, or after an unlock its admin.
    is_system_admin: bool


class AdminTenant(BaseModel):
    slug: str
    name: str


class Unlocked(BaseModel):
    #: When the unlock ends: the system admin is the tenant's admin until then.
    until: datetime


#: How long a system admin's unlock lasts, unless they ask for less.
UNLOCK_MINUTES = 60
#: Methods a read-only system admin may use.
_READS = {"GET", "HEAD", "OPTIONS"}


class TokenCreate(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    #: ``read`` unless the caller opts into ``edit``; fixed once made.
    scope: Scope = "read"
    expires_in_days: int = Field(DEFAULT_DAYS, ge=1, le=MAX_DAYS)


class TokenView(BaseModel):
    id: uuid.UUID
    name: str
    scope: Scope
    #: The token's first characters, to tell it apart; never the secret.
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class NewToken(TokenView):
    #: The secret: shown this once, and never kept.
    token: str


class MyTenant(BaseModel):
    slug: str
    name: str
    role: str
    is_admin: bool


# Must equal the devices_kind_check constraint (a test compares them).
DeviceKind = Literal["lite", "lite_flatport", "mobile", "multilens", "mono", "scout"]


class DeviceCreate(BaseModel):
    kind: DeviceKind
    serial: str


class Device(BaseModel):
    id: uuid.UUID
    kind: DeviceKind
    serial: str


def create_app(
    *,
    engine: AsyncEngine,
    validator: Validator,
    stores: TenantStores | None = None,
    geocoder: Geocoder | None = None,
) -> FastAPI:
    """``stores``: each tenant's own bucket; None means no tenant has one, so
    no tenant can sync mobile captures. ``geocoder``: our own, to name the
    places of captures the app couldn't; None, no names are derived."""
    app = FastAPI(title="FishSense Services API")

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422, except that it echoes the offending input, and
        # a NaN or Infinity there (which JSON can't hold) crashed it into a 500.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": _json_safe(jsonable_encoder(error.errors()))},
        )

    if stores is None:
        stores = TenantStores(
            endpoint_url="http://unset.invalid",
            public_endpoint_url="http://unset.invalid",
            region="unset",
            stores={},
        )
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        # The MCP endpoint's in-process reads: it resolved the token already.
        principal = request.scope.get(IN_PROCESS_PRINCIPAL)
        if principal is None:
            if credentials is None:
                raise _unauthorized()
            raw = credentials.credentials
            if not raw.startswith(TOKEN_PREFIX):
                try:
                    # In a threadpool, so a JWKS fetch never blocks the loop.
                    return await run_in_threadpool(validator.validate, raw)
                except InvalidToken:
                    raise _unauthorized() from None
                except KeysUnavailable:
                    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE) from None
            owner = await token_principal(engine, raw)
            if owner is None:
                raise _unauthorized()
            principal = Principal(sub=owner.sub, token_scope=owner.scope)
        # One place for every route: a read token is refused every write
        # request (0066). A GET keeps its own server-side effects: a system
        # admin's visit is recorded, and the Fish list names places it can.
        if principal.token_scope != EDIT and request.method not in _READS:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "read-only token: make an edit token to change anything",
            )
        return principal

    Caller = Annotated[Principal, Depends(authenticate)]

    async def membership(slug: str, principal: Caller, request: Request) -> Membership:
        # Provision and resolve in one transaction that *commits* before any
        # 404 is raised, so a newcomer's user row survives the refusal.
        async with principal_transaction(
            engine, principal.sub, org=principal.org
        ) as conn:
            await _first_sight(conn, principal)
            found = await resolve_membership(conn, principal.sub, slug)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        # One place for every route: a system admin who has not unlocked this
        # tenant reads it, and writes nothing (0056).
        if found.read_only and request.method not in _READS:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "read-only: a system admin must unlock this tenant to change it",
            )
        # An unlock is for its system admin, in the web app: their tokens
        # write only where they are a member.
        if (
            principal.token_scope is not None
            and found.access == UNLOCKED_ACCESS
            and request.method not in _READS
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "a token writes only in tenants its owner is a member of",
            )
        return found

    def signed_in(principal: Caller) -> Principal:
        """The caller, in an Authentik session: no token may unlock a tenant
        or manage tokens (0066)."""
        if principal.token_scope is not None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "sign in to the web app to do this"
            )
        return principal

    Session = Annotated[Principal, Depends(signed_in)]

    Member = Annotated[Membership, Depends(membership)]

    # Liveness for orchestration; no auth, and not part of the client API.
    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/me", operation_id="get_me", response_model=Me)
    async def me(principal: Caller) -> Me:
        async with principal_transaction(
            engine, principal.sub, org=principal.org
        ) as conn:
            await _first_sight(conn, principal)
            admin = (
                await conn.execute(text("SELECT caller_is_system_admin()"))
            ).scalar_one()
        return Me(is_system_admin=admin)

    @app.get(
        "/admin/tenants",
        operation_id="list_all_tenants",
        response_model=list[AdminTenant],
    )
    async def all_tenants(principal: Caller) -> list[AdminTenant]:
        """Every tenant, for a system admin's switcher; 404 for anyone else."""
        async with principal_transaction(
            engine, principal.sub, org=principal.org
        ) as conn:
            if not (
                await conn.execute(text("SELECT caller_is_system_admin()"))
            ).scalar_one():
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            rows = await conn.execute(text("SELECT * FROM system_admin_tenants()"))
            return [AdminTenant(**row._mapping) for row in rows]

    @app.post(
        "/tenants/{slug}/system-admin/unlock",
        operation_id="unlock_tenant",
        response_model=Unlocked,
    )
    async def unlock(slug: str, principal: Session) -> Unlocked:
        """A system admin, deliberately, becomes the admin of a tenant they are
        not in, for `UNLOCK_MINUTES`; recorded. 404 for anyone else, and for a
        tenant they are already a member of."""
        async with principal_transaction(
            engine, principal.sub, org=principal.org
        ) as conn:
            until = (
                await conn.execute(
                    text("SELECT system_admin_unlock(:slug, :minutes)"),
                    {"slug": slug, "minutes": UNLOCK_MINUTES},
                )
            ).scalar_one()
        if until is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return Unlocked(until=until)

    @app.get(
        "/me/tokens", operation_id="list_my_tokens", response_model=list[TokenView]
    )
    async def list_tokens(principal: Session) -> list[TokenView]:
        """The caller's personal API tokens, newest first: never their secrets."""
        async with principal_transaction(engine, principal.sub) as conn:
            return [TokenView(**vars(t)) for t in await my_tokens(conn)]

    @app.post(
        "/me/tokens",
        operation_id="create_my_token",
        response_model=NewToken,
        status_code=status.HTTP_201_CREATED,
    )
    async def make_token(body: TokenCreate, principal: Session) -> NewToken:
        """A token that acts as the caller, for a script or an agent: ``read``
        unless they ask for ``edit``. Its secret is in this response only."""
        async with principal_transaction(
            engine, principal.sub, org=principal.org
        ) as conn:
            await _first_sight(conn, principal)
            made = await create_token(
                conn, name=body.name, scope=body.scope, days=body.expires_in_days
            )
        if made is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "too many live tokens: revoke one you no longer use",
            )
        raw, token = made
        return NewToken(**vars(token), token=raw)

    @app.delete(
        "/me/tokens/{token_id}",
        operation_id="revoke_my_token",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def revoke(token_id: uuid.UUID, principal: Session) -> Response:
        async with principal_transaction(engine, principal.sub) as conn:
            if not await revoke_token(conn, token_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/me/memberships",
        operation_id="list_my_memberships",
        response_model=list[MyTenant],
    )
    async def my_memberships(principal: Caller) -> list[MyTenant]:
        """The tenants the caller belongs to (a partner's is their org's)."""
        async with principal_transaction(
            engine, principal.sub, org=principal.org
        ) as conn:
            await _first_sight(conn, principal)
            found = await list_memberships(conn, principal.sub)
        return [
            MyTenant(slug=m.slug, name=m.name, role=m.role, is_admin=m.is_admin)
            for m in found
        ]

    @app.get(
        "/tenants/{slug}/devices",
        operation_id="list_devices",
        response_model=list[Device],
    )
    async def list_devices(member: Member) -> list[Device]:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            rows = await conn.execute(
                text("""
                    SELECT id, kind, serial FROM devices
                    WHERE tenant_id = :tenant_id
                    ORDER BY serial
                    """),
                {"tenant_id": member.tenant_id},
            )
            return [Device(**row._mapping) for row in rows]

    @app.post(
        "/tenants/{slug}/devices",
        operation_id="create_device",
        response_model=Device,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_device(body: DeviceCreate, member: Member) -> Device:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                row = (
                    await conn.execute(
                        text("""
                            INSERT INTO devices (tenant_id, kind, serial)
                            VALUES (:tenant_id, :kind, :serial)
                            RETURNING id, kind, serial
                            """),
                        {"tenant_id": member.tenant_id, **body.model_dump()},
                    )
                ).one()
        except IntegrityError as error:
            if _is_unique_violation(error):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "a device with this serial already exists in this tenant",
                ) from None
            raise
        return Device(**row._mapping)

    add_portal_routes(app, engine=engine, membership=membership)
    add_mission_routes(app, engine=engine, membership=membership)
    # One namer for the capture page and the Fish list, the reads that retry
    # naming the phone captures the app couldn't name.
    namer = PlaceNamer(engine, geocoder)
    add_observation_routes(
        app, engine=engine, membership=membership, stores=stores, namer=namer
    )
    add_public_routes(app, engine=engine)
    add_automatic_routes(app, engine=engine, membership=membership, stores=stores)
    add_review_routes(app, engine=engine, membership=membership)
    add_mobile_routes(
        app,
        engine=engine,
        membership=membership,
        authenticate=authenticate,
        stores=stores,
        geocoder=geocoder,
        namer=namer,
    )
    # Agents' read-only door (0066): last, as it calls the routes above.
    mount_mcp(app, engine)
    return app


async def _first_sight(conn, principal: Principal) -> None:
    """Step 2: the caller's user row, with their name and email as their token
    gives them (0065), and their org's tenant if they have one."""
    await provision_user(conn, principal.sub)
    if principal.name or principal.email:
        await record_profile(conn, principal.name, principal.email)
    if principal.org is not None:
        await join_claimed_tenant(conn)


def _json_safe(value):
    """`value` with every NaN or Infinity spelled as text."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _is_unique_violation(error: IntegrityError) -> bool:
    return getattr(error.orig, "sqlstate", None) == UNIQUE_VIOLATION


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
    )
