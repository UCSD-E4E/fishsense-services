"""Observations over HTTP: every measured fish, Lite and Mobile alike.

PLAN.md §4.9, Phase 1 of §9.26. For any member of the tenant:

* ``GET /tenants/{slug}/observations`` -- newest first, a page at a time;
  ``?device=lite``, ``mobile`` or ``other`` narrows it. The three cover every
  fish once (``other``: any other kind of device, or none).
* ``GET /tenants/{slug}/observations/{capture_id}/{fish_id}`` -- one fish,
  with the evidence its method rests on.

A mobile fish carries a presigned link to its photo, from its tenant's own
bucket. A Lite fish has none yet: its frames are in the shared labels
bucket, which the API does not read, and Lite photos are to come into the
tenant's bucket as Mobile's do (a later phase).
"""

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Query,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.mobile_api import Point
from fishsense_services_api.observation_store import (
    Observation,
    observation_page,
    observation,
    observation_species,
    mission_captures,
)
from fishsense_services_api.place_naming import PlaceNamer
from fishsense_services_api.summary_store import tenant_summary
from fishsense_services_api.tenant_regions_store import tenant_regions
from fishsense_services_api.tenant_stores import NoTenantStore, TenantStores

__all__ = ["add_observation_routes"]


class DeviceView(BaseModel):
    kind: str | None
    name: str | None


class MissionView(BaseModel):
    number: int | None
    name: str | None


class SpeciesView(BaseModel):
    #: Either may be missing (0004): a species is named by what it has.
    scientific_name: str | None
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
    mission: MissionView | None
    species: SpeciesView | None
    fish_model: FishModelView | None
    location: Coordinates | None
    #: The app's place name, else the one the server derived (0041).
    place_name: str | None
    #: A mobile capture's uuid: its full record is ``/mobile/captures/{uuid}``.
    client_capture_uuid: uuid.UUID | None
    #: A presigned GET of the photo, valid for an hour: a phone's own image,
    #: or a Lite frame's render; None until a Lite frame is rendered.
    photo_url: str | None


class SpeciesCountView(BaseModel):
    scientific_name: str
    common_name: str | None
    #: The tenant's measured fish of it.
    count: int


class ObservationPage(BaseModel):
    observations: list[ObservationView]
    #: Pass as ``before`` for the next page; None at the end.
    next: str | None
    #: How many fish the filters keep in all, whatever the page or cursor.
    total: int


class LaserEvidenceView(BaseModel):
    depth_m: float | None
    range_m: float | None
    calibration_producer: str | None
    #: In the labeled frame's pixels.
    dot: Point | None
    head: Point | None
    tail: Point | None
    #: The labeler's color for the dot, ``red`` or ``green``, or None.
    color: str | None


class DevicePointsView(BaseModel):
    snout: Point
    fork: Point
    origin: str


class PhotoSize(BaseModel):
    width: int
    height: int


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
    #: The photo's size in pixels when it is the frame's render (a Lite
    #: fish), for drawing the labeled points on it; None otherwise.
    photo_size: PhotoSize | None = None


class TenantRegionView(BaseModel):
    """A broad region, at its one point on the map (`regions`)."""

    name: str
    latitude: float
    longitude: float
    fish: int


class TenantRegionsView(BaseModel):
    """Where a tenant's wild fish were measured, never finer than a region."""

    #: Most fish first.
    regions: list[TenantRegionView]
    #: Fish no region holds.
    elsewhere: int


class TenantSummaryView(BaseModel):
    """A tenant's numbers (docs/design/README.md, Home pages)."""

    lengths: int
    #: Lengths of a species, not of a fish model (a decoy of known length).
    wild_fish: int
    species: int
    missions: int
    deployments: int
    #: Canonical captures: a frame copied into several dumps counts once.
    frames: int
    #: Missions whose camera port nobody has stated: the stages refuse them.
    unknown_ports: int
    #: The newest capture's arrival: an ingest or a sync.
    latest_activity: datetime | None


def add_observation_routes(
    app: FastAPI,
    *,
    engine: AsyncEngine,
    membership,
    stores: TenantStores,
    namer: PlaceNamer | None = None,
) -> None:
    """``membership`` is the app's dependency; ``stores`` signs photo links;
    ``namer`` names the phone captures on a page that still have no name."""

    Member = Annotated[Membership, Depends(membership)]

    def bucket(slug: str) -> str | None:
        """The tenant's current bucket: a Lite photo is a render there."""
        try:
            return stores.for_tenant(slug).bucket
        except NoTenantStore:
            return None

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
            mission=(
                MissionView(number=found.mission_number, name=found.mission_name)
                if found.mission_number is not None or found.mission_name is not None
                else None
            ),
            species=(
                SpeciesView(
                    scientific_name=found.scientific_name,
                    common_name=found.common_name,
                )
                if found.scientific_name is not None or found.common_name is not None
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
        "/tenants/{slug}/summary",
        operation_id="get_tenant_summary",
        response_model=TenantSummaryView,
    )
    async def summary(member: Member) -> TenantSummaryView:
        """The tenant's numbers, for its Today page (`summary_store`)."""
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await tenant_summary(conn, member.tenant_id)
        return TenantSummaryView(**asdict(found))

    @app.get(
        "/tenants/{slug}/regions",
        operation_id="get_tenant_regions",
        response_model=TenantRegionsView,
    )
    async def regions(member: Member) -> TenantRegionsView:
        """Where the tenant's wild fish were measured, by broad region, for
        its Today map (`tenant_regions_store`). Fish models are left off."""
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await tenant_regions(conn, member.tenant_id)
        return TenantRegionsView(
            regions=[TenantRegionView(**asdict(r)) for r in found.regions],
            elsewhere=found.elsewhere,
        )

    @app.get(
        "/tenants/{slug}/missions/{number}/captures",
        operation_id="list_mission_captures",
        response_model=CaptureCardPage,
    )
    async def list_mission_captures(
        slug: str,
        number: Annotated[int, Path(ge=0)],
        member: Member,
        limit: Annotated[int, Query(ge=1, le=200)] = 48,
        after: str | None = None,
    ) -> CaptureCardPage:
        """Every capture of the mission, measured or not, in the order taken."""
        cursor = _parse_capture_cursor(after) if after else None
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await mission_captures(
                conn,
                member.tenant_id,
                number,
                limit=limit + 1,
                after=cursor,
                photo_bucket=bucket(slug),
            )
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such mission")
        page, more = found[:limit], len(found) > limit
        try:
            objects = stores.for_tenant(slug)
        except NoTenantStore:
            objects = None
        return CaptureCardPage(
            captures=[
                CaptureCardView(
                    capture_id=c.capture_id,
                    client_capture_uuid=c.client_capture_uuid,
                    captured_at=c.captured_at,
                    photo_url=(
                        objects.presign_get(c.photo_key)
                        if objects is not None and c.photo_key
                        else None
                    ),
                    fish=[MeasuredFish(fish_id=f, length_m=n) for f, n in c.fish],
                )
                for c in page
            ],
            next=_capture_cursor(page[-1].cursor) if more else None,
        )

    @app.get(
        "/tenants/{slug}/observations/species",
        operation_id="list_observation_species",
        response_model=list[SpeciesCountView],
    )
    async def list_species(
        member: Member,
        device: Literal["lite", "mobile", "other"] | None = None,
    ) -> list[SpeciesCountView]:
        """The species the tenant's measured fish are, most first, with how
        many of each: what the Fish page offers to filter by. Only the
        tenant's own (the owner, 2026-10-23)."""
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await observation_species(conn, member.tenant_id, device=device)
        return [SpeciesCountView(**vars(s)) for s in found]

    @app.get(
        "/tenants/{slug}/observations",
        operation_id="list_observations",
        response_model=ObservationPage,
    )
    async def list_(
        slug: str,
        member: Member,
        background: BackgroundTasks,
        device: Literal["lite", "mobile", "other"] | None = None,
        mission: Annotated[int | None, Query(ge=0)] = None,
        species: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: str | None = None,
        page: Annotated[int | None, Query(ge=1)] = None,
    ) -> ObservationPage:
        """Newest first, a page at a time: by number (``page``, from 1, of
        ``limit`` each) or after a cursor (``before``, the last page's
        ``next``), not both. ``total`` is how many there are in all."""
        if page is not None and before is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a page or a cursor, not both",
            )
        cursor = _parse_cursor(before) if before else None
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await observation_page(
                conn,
                member.tenant_id,
                limit=limit + 1,
                offset=((page or 1) - 1) * limit,
                before=cursor,
                device=device,
                photo_bucket=bucket(slug),
                mission=mission,
                species=species,
            )
        listed, more = found.observations[:limit], len(found.observations) > limit
        if namer is not None:
            namer.later(
                background,
                member.tenant_id,
                [
                    o.capture_id
                    for o in listed
                    if o.place_name is None and o.derived_place_name is None
                ],
            )
        return ObservationPage(
            observations=[view(slug, o) for o in listed],
            next=_cursor(listed[-1].cursor) if more else None,
            total=found.total,
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
            found = await observation(
                conn, member.tenant_id, capture_id, fish_id, photo_bucket=bucket(slug)
            )
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
                    color=laser.color,
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
            photo_size=(
                PhotoSize(width=found.photo_size[0], height=found.photo_size[1])
                if found.photo_size
                else None
            ),
        )


def _point(xy: tuple[float, float] | None) -> Point | None:
    return None if xy is None else Point(x=xy[0], y=xy[1])


class MeasuredFish(BaseModel):
    fish_id: uuid.UUID
    length_m: float | None


class CaptureCardView(BaseModel):
    capture_id: uuid.UUID
    client_capture_uuid: uuid.UUID | None
    captured_at: datetime
    photo_url: str | None
    #: Empty for a capture with no measured fish.
    fish: list[MeasuredFish]


class CaptureCardPage(BaseModel):
    captures: list[CaptureCardView]
    #: Pass as ``after`` for the next page; None at the end.
    next: str | None


def _capture_cursor(position: tuple[datetime, uuid.UUID]) -> str:
    return f"{position[0].isoformat()}~{position[1]}"


def _parse_capture_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        at, capture_id = value.split("~")
        return datetime.fromisoformat(at), uuid.UUID(capture_id)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "not a page cursor"
        ) from None


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
