"""The HTTP API.

Each request goes through the same four steps before touching tenant data:

1. authenticate the bearer token in-app (401; 503 if the IdP's keys can't be
   fetched -- an outage is not a bad token);
2. provision the caller's user row on first sight;
3. resolve their membership in the tenant named by the path (404 for both
   "not a member" and "no such tenant", so tenants can't be probed);
4. run the tenant work in a tenant-scoped transaction, under RLS.
"""

import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.auth import (
    InvalidToken,
    KeysUnavailable,
    Principal,
    TokenValidator,
)
from fishsense_services_api.db import principal_transaction, tenant_transaction
from fishsense_services_api.memberships import Membership, resolve_membership
from fishsense_services_api.users import provision_user


class DeviceCreate(BaseModel):
    kind: str
    serial: str


class Device(BaseModel):
    id: uuid.UUID
    kind: str
    serial: str


def create_app(*, engine: AsyncEngine, validator: TokenValidator) -> FastAPI:
    app = FastAPI(title="FishSense Services API")
    bearer = HTTPBearer(auto_error=False)

    # Deliberately sync: FastAPI runs it in a threadpool, so a JWKS fetch
    # never blocks the event loop.
    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is None:
            raise _unauthorized()
        try:
            return validator.validate(credentials.credentials)
        except InvalidToken:
            raise _unauthorized() from None
        except KeysUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE) from None

    async def membership(
        slug: str, principal: Annotated[Principal, Depends(authenticate)]
    ) -> Membership:
        # Provision and resolve in one transaction that *commits* before any
        # 404 is raised, so a newcomer's user row survives the refusal.
        async with principal_transaction(engine, principal.sub) as conn:
            await provision_user(conn, principal.sub)
            found = await resolve_membership(conn, principal.sub, slug)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return found

    Member = Annotated[Membership, Depends(membership)]

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
            return Device(**row._mapping)

    return app


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
    )
