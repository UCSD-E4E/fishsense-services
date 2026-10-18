"""Observations over HTTP: every measured fish, Lite and Mobile alike.

PLAN.md §4.9, Phase 1 of §9.26. For any member of the tenant:

* ``GET /tenants/{slug}/observations`` -- newest first, a page at a time;
  ``?device=lite`` or ``?device=mobile`` narrows it.
* ``GET /tenants/{slug}/observations/{capture_id}/{fish_id}`` -- one fish,
  with the evidence its method rests on.

A mobile fish carries a presigned link to its photo, from its tenant's own
bucket. A Lite fish has none yet: its frames are in the shared labels
bucket, which the API does not read, and Lite photos are to come into the
tenant's bucket as Mobile's do (a later phase).
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.mobile_api import Point
from fishsense_services_api.observation_store import (
    Observation,
    list_observations,
    observation,
)
from fishsense_services_api.tenant_stores import NoTenantStore, TenantStores

__all__ = ["add_observation_routes"]

#: The device kinds each ``?device=`` value means: Lite is every Lite rig.
DEVICE_KINDS = {"lite": ("lite", "lite_flatport"), "mobile": ("mobile",)}


class DeviceView(BaseModel):
    kind: str | None
    name: str | None


class DiveView(BaseModel):
    number: int | None
    name: str | None


class SpeciesView(BaseModel):
    scientific_name: str
    common_name: str | None


class FishModelView(BaseModel):
    name: str
    #: The model's current known length: what its measurement is judged by.
    known_length_m: float | None


class Coordinates(BaseModel):
    latitude: float
    longitude: float


class ObservationView(BaseModel):
    capture_id: uuid.UUID
    fish_id: uuid.UUID
    #: How the depth was found: ``laser`` (Lite) or ``lidar`` (Mobile).
    method: str
    length_m: float | None
    captured_at: datetime
    device: DeviceView
    dive: DiveView | None
    species: SpeciesView | None
    fish_model: FishModelView | None
    location: Coordinates | None
    #: The app's place name, else the one the server derived (0041).
    place_name: str | None
    #: A mobile capture's uuid: its full record is ``/mobile/captures/{uuid}``.
    client_capture_uuid: uuid.UUID | None
    #: A presigned GET of the photo, valid for an hour; None for Lite (yet).
    photo_url: str | None


class ObservationPage(BaseModel):
    observations: list[ObservationView]
    #: Pass as ``before`` for the next page; None at the end.
    next: str | None


class LaserEvidenceView(BaseModel):
    depth_m: float | None
    range_m: float | None
    calibration_producer: str | None
    #: In the labelled frame's pixels.
    dot: Point | None
    head: Point | None
    tail: Point | None


class DevicePointsView(BaseModel):
    snout: Point
    fork: Point
    origin: str


class ObservationDetailView(BaseModel):
    observation: ObservationView
    measured_at: datetime
    algorithm: str | None
    algorithm_version: str | None
    core_version: str | None
    model_version: str | None
    #: The laser path's inputs (``method == "laser"``).
    laser: LaserEvidenceView | None
    #: The phone's snout and fork (``method == "lidar"``).
    device_points: DevicePointsView | None


def add_observation_routes(
    app: FastAPI,
    *,
    engine: AsyncEngine,
    membership,
    stores: TenantStores,
) -> None:
    """``membership`` is the app's dependency; ``stores`` signs photo links."""

    Member = Annotated[Membership, Depends(membership)]

    def photo_url(slug: str, found: Observation) -> str | None:
        if found.photo_key is None:
            return None
        try:
            return stores.for_tenant(slug).presign_get(found.photo_key)
        except NoTenantStore:
            return None

    def view(slug: str, found: Observation) -> ObservationView:
        return ObservationView(
            capture_id=found.capture_id,
            fish_id=found.fish_id,
            method=found.method,
            length_m=found.length_m,
            captured_at=found.captured_at,
            device=DeviceView(kind=found.device_kind, name=found.device_name),
            dive=(
                DiveView(number=found.dive_number, name=found.dive_name)
                if found.dive_number is not None or found.dive_name is not None
                else None
            ),
            species=(
                SpeciesView(
                    scientific_name=found.scientific_name,
                    common_name=found.common_name,
                )
                if found.scientific_name is not None
                else None
            ),
            fish_model=(
                FishModelView(
                    name=found.fish_model, known_length_m=found.known_length_m
                )
                if found.fish_model is not None
                else None
            ),
            location=(
                Coordinates(latitude=found.latitude, longitude=found.longitude)
                if found.latitude is not None and found.longitude is not None
                else None
            ),
            place_name=found.place_name or found.derived_place_name,
            client_capture_uuid=found.client_capture_uuid,
            photo_url=photo_url(slug, found),
        )

    @app.get(
        "/tenants/{slug}/observations",
        operation_id="list_observations",
        response_model=ObservationPage,
    )
    async def list_(
        slug: str,
        member: Member,
        device: Literal["lite", "mobile"] | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: str | None = None,
    ) -> ObservationPage:
        cursor = _parse_cursor(before) if before else None
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await list_observations(
                conn,
                member.tenant_id,
                limit=limit + 1,
                before=cursor,
                device_kinds=DEVICE_KINDS[device] if device else None,
            )
        page, more = found[:limit], len(found) > limit
        return ObservationPage(
            observations=[view(slug, o) for o in page],
            next=_cursor(page[-1].cursor) if more else None,
        )

    @app.get(
        "/tenants/{slug}/observations/{capture_id}/{fish_id}",
        operation_id="get_observation",
        response_model=ObservationDetailView,
    )
    async def get(
        slug: str, capture_id: uuid.UUID, fish_id: uuid.UUID, member: Member
    ) -> ObservationDetailView:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await observation(conn, member.tenant_id, capture_id, fish_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        laser = found.laser
        points = found.device_points
        return ObservationDetailView(
            observation=view(slug, found.observation),
            measured_at=found.measured_at,
            algorithm=found.algorithm,
            algorithm_version=found.algorithm_version,
            core_version=found.core_version,
            model_version=found.model_version,
            laser=(
                LaserEvidenceView(
                    depth_m=laser.depth_m,
                    range_m=laser.range_m,
                    calibration_producer=laser.calibration_producer,
                    dot=_point(laser.dot),
                    head=_point(laser.head),
                    tail=_point(laser.tail),
                )
                if laser is not None
                else None
            ),
            device_points=(
                DevicePointsView(
                    snout=_point(points.snout),
                    fork=_point(points.fork),
                    origin=points.origin,
                )
                if points is not None
                else None
            ),
        )


def _point(xy: tuple[float, float] | None) -> Point | None:
    return None if xy is None else Point(x=xy[0], y=xy[1])


def _cursor(position: tuple[datetime, uuid.UUID, uuid.UUID]) -> str:
    return f"{position[0].isoformat()}~{position[1]}~{position[2]}"


def _parse_cursor(value: str) -> tuple[datetime, uuid.UUID, uuid.UUID]:
    try:
        at, capture_id, fish_id = value.split("~")
        return datetime.fromisoformat(at), uuid.UUID(capture_id), uuid.UUID(fish_id)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "not a page cursor"
        ) from None
