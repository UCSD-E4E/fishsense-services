"""A phone capture's server measurement over HTTP, for the web (the owner,
2026-11-10: the CCFRP lengths, shown unvalidated).

Read-only, for any member of the tenant. The result is the server's,
measured as FishSense Mobile measures (`phone_measurement_store`), and the web
shows it as unvalidated: it is never a measured fish until a person reviews
it. The capture's JPEG and mask go out as presigned links when they are in the
tenant's current bucket; the mask's bytes too, which the web serves from its
own origin (as `automatic_api`'s mask) to tint the fish. A capture the caller
can't see, or the server never measured, is 404.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.phone_measurement_store import current_phone_measurement
from fishsense_services_api.tenant_stores import NoTenantStore, TenantStores

__all__ = ["PhonePoint", "PhoneMeasurementView", "add_phone_measurement_routes"]


class PhonePoint(BaseModel):
    #: Full-frame pixels of the capture's JPEG.
    x: float
    y: float


class PhoneMeasurementView(BaseModel):
    #: ``measured``, or why not: ``no_fish``, ``headtail_failed``,
    #: ``depth_failed``, ``image_unavailable``, ``decode_failed``.
    status: str
    length_m: float | None
    snout: PhonePoint | None
    fork: PhonePoint | None
    snout_depth_m: float | None
    fork_depth_m: float | None
    photo_url: str | None
    #: A frame-sized grayscale PNG, 255 on the fish.
    mask_url: str | None
    mask_width: int | None
    mask_height: int | None
    model_id: str | None
    core_version: str | None
    #: The app commit the measurement follows (``fishsense-mobile@<sha>``).
    algorithm: str
    error: str | None
    measured_at: datetime


def _point(x: float | None, y: float | None) -> PhonePoint | None:
    return None if x is None or y is None else PhonePoint(x=x, y=y)


def add_phone_measurement_routes(
    app: FastAPI, *, engine: AsyncEngine, membership, stores: TenantStores
) -> None:
    Member = Annotated[Membership, Depends(membership)]

    def objects_for(slug: str):
        try:
            return stores.for_tenant(slug)
        except NoTenantStore:
            return None

    def link(slug: str, bucket: str | None, key: str | None) -> str | None:
        objects = objects_for(slug)
        if objects is None or bucket is None or key is None:
            return None
        return objects.presign_get(key) if objects.bucket == bucket else None

    @app.get(
        "/tenants/{slug}/captures/{capture_id}/phone-measurement",
        operation_id="get_phone_measurement",
        response_model=PhoneMeasurementView,
    )
    async def get_phone_measurement(
        slug: str, capture_id: uuid.UUID, member: Member
    ) -> PhoneMeasurementView:
        """The capture's current server measurement. Unvalidated."""
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await current_phone_measurement(conn, member.tenant_id, capture_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no phone measurement")
        return PhoneMeasurementView(
            status=found.status,
            length_m=found.length_m,
            snout=_point(found.snout_x, found.snout_y),
            fork=_point(found.fork_x, found.fork_y),
            snout_depth_m=found.snout_depth_m,
            fork_depth_m=found.fork_depth_m,
            photo_url=link(slug, found.photo_bucket, found.photo_key),
            mask_url=link(slug, found.mask_bucket, found.mask_key),
            mask_width=found.mask_width,
            mask_height=found.mask_height,
            model_id=found.model_id,
            core_version=found.core_version,
            algorithm=found.algorithm,
            error=found.error,
            measured_at=found.created_at,
        )

    @app.get(
        "/tenants/{slug}/captures/{capture_id}/phone-measurement/mask",
        operation_id="get_phone_measurement_mask",
        response_class=Response,
        responses={200: {"content": {"image/png": {}}}},
    )
    async def get_phone_mask(
        slug: str, capture_id: uuid.UUID, member: Member
    ) -> Response:
        """The current server measurement's mask, as its PNG bytes."""
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await current_phone_measurement(conn, member.tenant_id, capture_id)
        if found is None or found.mask_key is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no mask for this capture")
        objects = objects_for(slug)
        data = (
            await run_in_threadpool(objects.get, found.mask_key)
            if objects is not None and objects.bucket == found.mask_bucket
            else None
        )
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no mask for this capture")
        return Response(
            data,
            media_type="image/png",
            # Written once per result; private: a member's.
            headers={"cache-control": "private, max-age=3600"},
        )
