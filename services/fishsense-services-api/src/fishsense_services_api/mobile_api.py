"""FishSense Mobile sync and display (PLAN.md §9.6), over HTTP.

The protocol, for one capture the app took (``client_capture_uuid``):

1. ``POST /tenants/{slug}/mobile/captures/{uuid}/uploads`` -- the app declares
   each file it saved (role, SHA-256, size) and gets presigned PUTs into the
   tenant's own bucket (tenant_stores.py). Garage accepts exactly the declared
   bytes and nothing else.
2. The app PUTs the files straight to Garage; the API never sees bytes.
3. ``PUT /tenants/{slug}/mobile/captures/{uuid}`` -- commit: everything the
   app recorded about the capture. Every file is read back from the store
   (SHA-256 and size) and must match its geometry before anything is kept; a
   capture exists only once committed. Idempotent on the uuid.
4. ``POST .../{uuid}/measurements`` -- a redone two-point selection: a new
   measurement version, idempotent on the app's measurement uuid.

Display, for any member of the tenant: ``GET /tenants/{slug}/mobile/captures``
(newest first, paged) and ``GET .../{uuid}`` (everything, with presigned
links to every file).

The app requires a sign-in and records, with every capture, who was signed
in (``captured_by_sub``). Only that user may sync it: a different login on
the same phone gets 403, and a uuid another user already committed is 409.
A tenant with no bucket of its own (§9.11) can't upload at all (409).

Reproducibility is the point -- this is scientific data. So the commit keeps
every file (``rgb_lossless``, the frame the phone measured on, whenever the
app sends it), every number, and the request as received (``manifest``), so a
field this API doesn't model yet isn't lost.
"""

import asyncio
import base64
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.auth import Principal
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.place_names import Geocoder
from fishsense_services_api.place_naming import PlaceNamer
from fishsense_services_api.mobile_capture_store import (
    CaptureConflict,
    CaptureDetail,
    CaptureFile,
    DeviceMeasurement,
    MobileCapture,
    NoSuchCapture,
    StoredMeasurement,
    append_device_measurement,
    commit_capture,
    committed_by,
    mobile_capture,
)
from fishsense_services_api.tenant_stores import (
    NoTenantStore,
    TenantObjects,
    TenantStores,
)

MB = 1 << 20
#: At most this many captures are named per request, so one page view can't
#: hand the geocoder a backlog.


@dataclass(frozen=True)
class _Role:
    content_type: str
    suffix: str
    max_bytes: int


#: Every kind of file a mobile capture may carry. ``rgb_lossless`` is the
#: frame the phone measured on, losslessly: what makes a server recompute
#: exact (docs/handoff/fishsense-mobile).
ROLES: dict[str, _Role] = {
    "rgb_jpeg": _Role("image/jpeg", ".jpg", 64 * MB),
    "rgb_lossless": _Role("image/png", ".png", 256 * MB),
    "depth_f32": _Role("application/octet-stream", ".f32", 16 * MB),
    "confidence_u8": _Role("application/octet-stream", ".u8", 16 * MB),
    "mask_u8": _Role("application/octet-stream", ".u8", 64 * MB),
}
REQUIRED_ROLES = frozenset({"rgb_jpeg", "depth_f32", "confidence_u8"})
Role = Literal["rgb_jpeg", "rgb_lossless", "depth_f32", "confidence_u8", "mask_u8"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Finite(BaseModel):
    """JSON has no NaN or Infinity, and neither may a measurement."""

    model_config = ConfigDict(allow_inf_nan=False)


class DeclaredFile(BaseModel):
    role: Role
    sha256: Sha256
    size: int = Field(ge=0)

    @model_validator(mode="after")
    def _not_too_big(self) -> "DeclaredFile":
        if self.size > ROLES[self.role].max_bytes:
            raise ValueError(f"{self.role} is larger than {ROLES[self.role].max_bytes}")
        return self


def _distinct_roles(files: list[DeclaredFile]) -> None:
    roles = [f.role for f in files]
    if len(roles) != len(set(roles)):
        raise ValueError("each role at most once")


class UploadRequest(BaseModel):
    files: list[DeclaredFile] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self) -> "UploadRequest":
        _distinct_roles(self.files)
        return self


class Upload(BaseModel):
    role: Role
    url: str
    method: Literal["PUT"] = "PUT"
    #: Send exactly these; the signature covers them.
    headers: dict[str, str]


class UploadGrant(BaseModel):
    already_committed: bool
    uploads: list[Upload]


class Point(_Finite):
    x: float
    y: float


class MeasurementVersion(_Finite):
    client_measurement_uuid: uuid.UUID
    measured_at: datetime
    length_m: float = Field(gt=0)
    snout: Point
    fork: Point
    #: `auto`: the phone found the points; `manual`: the user placed them;
    #: `unknown`: a version from before the app recorded which (its v7
    #: overwrote edits in place).
    origin: Literal["auto", "manual", "unknown"]
    #: Null only for an `unknown` version, which predates provenance.
    algorithm: str | None = None
    algorithm_version: str | None = None
    core_version: str | None = None
    model_version: str | None = None

    @model_validator(mode="after")
    def _known_origin_names_its_algorithm(self) -> "MeasurementVersion":
        if self.origin != "unknown" and not (self.algorithm and self.algorithm_version):
            raise ValueError(f"an {self.origin} version names its algorithm")
        return self


class DeviceInfo(BaseModel):
    #: A stable id for this install of the app (one phone, one device row).
    install_id: str = Field(min_length=1, max_length=200)
    model: str | None = None
    info: str | None = None


class Location(_Finite):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    horizontal_accuracy_m: float | None = Field(default=None, ge=0)
    place_name: str | None = None


class DepthGeometry(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    #: ARKit pads rows; the app copies ``bytesPerRow``.
    row_bytes: int = Field(gt=0)


class ConfidenceGeometry(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    row_bytes: int | None = Field(default=None, gt=0)


class MaskGeometry(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class CaptureCommit(_Finite):
    """Everything the app recorded about one capture. Fields this API doesn't
    model are allowed, and kept in the manifest."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    captured_at: datetime
    #: The IANA zone the phone was in ("America/Los_Angeles"): its mission is
    #: named for the local day (0057). Without it, the day is UTC's.
    time_zone: str | None = None
    #: Who was signed in on the phone when it took the capture.
    captured_by_sub: str
    device: DeviceInfo
    #: Null for a capture from before the app recorded it.
    app_version: str | None = None
    core_version: str | None = None
    model_version: str | None = None
    location: Location | None = None
    orientation: Literal["portrait", "landscape"] | None = None
    #: Row-major K, 3x3.
    intrinsics: list[float] = Field(min_length=9, max_length=9)
    #: Row-major camera-to-world, 4x4, when the app records one.
    camera_pose: list[float] | None = Field(default=None, min_length=16, max_length=16)
    depth: DepthGeometry
    confidence: ConfidenceGeometry
    mask: MaskGeometry | None = None
    files: list[DeclaredFile]
    measurements: list[MeasurementVersion] = []

    @field_validator("time_zone")
    @classmethod
    def _a_known_zone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"not an IANA time zone: {value!r}") from None
        return value

    @model_validator(mode="after")
    def _adds_up(self) -> "CaptureCommit":
        _distinct_roles(self.files)
        if not _finite(self.model_extra):
            raise ValueError("a field holds NaN or Infinity, which JSON doesn't")
        sizes = {f.role: f.size for f in self.files}
        missing = REQUIRED_ROLES - sizes.keys()
        if missing:
            raise ValueError(f"missing files: {sorted(missing)}")
        if ("mask_u8" in sizes) != (self.mask is not None):
            raise ValueError("a mask file and its geometry come together")
        depth = self.depth
        if depth.row_bytes < depth.width * 4:
            raise ValueError("depth row_bytes is shorter than a row of float32")
        if sizes["depth_f32"] != depth.row_bytes * depth.height:
            raise ValueError("depth_f32 is not row_bytes x height bytes")
        confidence = self.confidence
        row = confidence.row_bytes or confidence.width
        if row < confidence.width or sizes["confidence_u8"] != row * confidence.height:
            raise ValueError("confidence_u8 is not row_bytes x height bytes")
        if self.mask is not None and sizes["mask_u8"] != (
            self.mask.width * self.mask.height
        ):
            raise ValueError("mask_u8 is not width x height bytes")
        return self


class FileView(BaseModel):
    role: str
    sha256: str
    size: int
    content_type: str
    #: A presigned GET, valid for an hour.
    url: str | None


class MeasurementView(BaseModel):
    client_measurement_uuid: uuid.UUID
    measured_at: datetime
    length_m: float
    snout: Point
    fork: Point
    origin: str
    algorithm: str | None
    algorithm_version: str | None
    core_version: str | None
    model_version: str | None


class CaptureView(BaseModel):
    client_capture_uuid: uuid.UUID
    #: The server's ids for the capture and its fish, as the Fish list gives
    #: them: a fish's page is addressed by both (phase 7b).
    capture_id: uuid.UUID
    fish_id: uuid.UUID | None
    #: Where an imported capture came from (0058); None for the app's sync.
    imported_from: str | None = None
    captured_at: datetime
    device: DeviceInfo
    app_version: str | None
    core_version: str | None
    model_version: str | None
    location: Location | None
    orientation: str | None
    intrinsics: list[float]
    camera_pose: list[float] | None
    depth: DepthGeometry
    confidence: ConfidenceGeometry
    mask: MaskGeometry | None
    files: list[FileView]
    #: Oldest first; the last is current.
    measurements: list[MeasurementView]
    current_measurement: MeasurementView | None
    manifest: dict
    #: The place name the server derived from the coordinates when the app
    #: sent none (0041); filled in the background, so it may come later.
    derived_place_name: str | None = None


def add_mobile_routes(
    app: FastAPI,
    *,
    engine: AsyncEngine,
    membership,
    authenticate,
    stores: TenantStores,
    geocoder: Geocoder | None = None,
    namer: PlaceNamer | None = None,
) -> None:
    """``membership`` and ``authenticate`` are the app's dependencies;
    ``geocoder`` names captures the app couldn't (None: no derived names), by
    way of ``namer`` when the app shares one."""

    name_later = (namer or PlaceNamer(engine, geocoder)).later

    Member = Annotated[Membership, Depends(membership)]
    Caller = Annotated[Principal, Depends(authenticate)]

    def tenant_objects(slug: str) -> TenantObjects:
        try:
            return stores.for_tenant(slug)
        except NoTenantStore:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this tenant has no storage of its own yet, so it can't sync",
            ) from None

    def readable(slug: str) -> TenantObjects | None:
        try:
            return stores.for_tenant(slug)
        except NoTenantStore:
            return None

    @app.post(
        "/tenants/{slug}/mobile/captures/{client_capture_uuid}/uploads",
        operation_id="request_mobile_capture_uploads",
        response_model=UploadGrant,
    )
    async def request_uploads(
        slug: str,
        client_capture_uuid: uuid.UUID,
        body: UploadRequest,
        member: Member,
    ) -> UploadGrant:
        objects = tenant_objects(slug)
        async with tenant_transaction(engine, member.tenant_id) as conn:
            capturer = await committed_by(conn, member.tenant_id, client_capture_uuid)
        if capturer is not None:
            if capturer != member.user_id:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "another user committed this capture"
                )
            return UploadGrant(already_committed=True, uploads=[])
        uploads = []
        for f in body.files:
            role = ROLES[f.role]
            signed = objects.presign_put(
                _key(client_capture_uuid, f.role),
                sha256=_b64(f.sha256),
                size=f.size,
                content_type=role.content_type,
            )
            uploads.append(Upload(role=f.role, url=signed.url, headers=signed.headers))
        return UploadGrant(already_committed=False, uploads=uploads)

    @app.put(
        "/tenants/{slug}/mobile/captures/{client_capture_uuid}",
        operation_id="commit_mobile_capture",
        response_model=CaptureView,
        responses={201: {"model": CaptureView}},
    )
    async def commit(
        slug: str,
        client_capture_uuid: uuid.UUID,
        body: CaptureCommit,
        member: Member,
        principal: Caller,
        response: Response,
        background: BackgroundTasks,
    ) -> CaptureView:
        if body.captured_by_sub != principal.sub:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "this capture was taken by another account; only it can sync it",
            )
        objects = tenant_objects(slug)
        files = await _verified(objects, client_capture_uuid, body.files)
        capture = MobileCapture(
            client_capture_uuid=client_capture_uuid,
            uploaded_by=member.user_id,
            captured_at=body.captured_at,
            device_install_id=body.device.install_id,
            device_model=body.device.model,
            device_info=body.device.info,
            app_version=body.app_version,
            core_version=body.core_version,
            model_version=body.model_version,
            latitude=body.location.latitude if body.location else None,
            longitude=body.location.longitude if body.location else None,
            horizontal_accuracy_m=(
                body.location.horizontal_accuracy_m if body.location else None
            ),
            place_name=body.location.place_name if body.location else None,
            orientation=body.orientation,
            intrinsics=tuple(body.intrinsics),
            camera_pose=None if body.camera_pose is None else tuple(body.camera_pose),
            depth_width=body.depth.width,
            depth_height=body.depth.height,
            depth_row_bytes=body.depth.row_bytes,
            confidence_width=body.confidence.width,
            confidence_height=body.confidence.height,
            mask_width=body.mask.width if body.mask else None,
            mask_height=body.mask.height if body.mask else None,
            files=files,
            manifest=body.model_dump(mode="json"),
        )
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                committed = await commit_capture(
                    conn,
                    member.tenant_id,
                    capture,
                    [_measurement(m) for m in body.measurements],
                )
                detail = await mobile_capture(
                    conn, member.tenant_id, client_capture_uuid
                )
        except CaptureConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        if committed.created:
            response.status_code = status.HTTP_201_CREATED
        if detail.place_name is None and detail.derived_place_name is None:
            name_later(background, member.tenant_id, [detail.capture_id])
        return _view(detail, objects)

    @app.post(
        "/tenants/{slug}/mobile/captures/{client_capture_uuid}/measurements",
        operation_id="add_mobile_measurement_version",
        response_model=MeasurementView,
        responses={201: {"model": MeasurementView}},
    )
    async def add_version(
        client_capture_uuid: uuid.UUID,
        body: MeasurementVersion,
        member: Member,
        response: Response,
    ) -> MeasurementView:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                appended = await append_device_measurement(
                    conn,
                    member.tenant_id,
                    client_capture_uuid,
                    member.user_id,
                    _measurement(body),
                )
        except NoSuchCapture:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except CaptureConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        if appended.created:
            response.status_code = status.HTTP_201_CREATED
        return _measurement_view(appended.measurement)

    @app.get(
        "/tenants/{slug}/mobile/captures/{client_capture_uuid}",
        operation_id="get_mobile_capture",
        response_model=CaptureView,
    )
    async def get_capture(
        slug: str,
        client_capture_uuid: uuid.UUID,
        member: Member,
        background: BackgroundTasks,
    ) -> CaptureView:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            detail = await mobile_capture(conn, member.tenant_id, client_capture_uuid)
        if detail is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        if detail.place_name is None and detail.derived_place_name is None:
            name_later(background, member.tenant_id, [detail.capture_id])
        return _view(detail, readable(slug))


def _key(client_capture_uuid: uuid.UUID, role: str) -> str:
    return f"mobile/captures/{client_capture_uuid}/{role}{ROLES[role].suffix}"


def _b64(hex_sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_sha256)).decode()


async def _verified(
    objects: TenantObjects,
    client_capture_uuid: uuid.UUID,
    declared: list[DeclaredFile],
) -> tuple[CaptureFile, ...]:
    """Each declared file, as the store holds it -- or 409 naming the first
    that is missing or isn't what was declared."""
    keys = [_key(client_capture_uuid, f.role) for f in declared]
    stored = await asyncio.gather(
        *(asyncio.to_thread(objects.head, key) for key in keys)
    )
    files = []
    for f, key, found in zip(declared, keys, stored, strict=True):
        if found is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"{f.role} has not been uploaded"
            )
        if found.size != f.size or found.sha256 != _b64(f.sha256):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{f.role} in the store is not the file declared",
            )
        files.append(
            CaptureFile(
                role=f.role,
                bucket=objects.bucket,
                key=key,
                sha256=f.sha256,
                size=f.size,
                content_type=ROLES[f.role].content_type,
            )
        )
    return tuple(files)


def _measurement(m: MeasurementVersion) -> DeviceMeasurement:
    return DeviceMeasurement(
        client_measurement_uuid=m.client_measurement_uuid,
        measured_at=m.measured_at,
        length_m=m.length_m,
        snout=(m.snout.x, m.snout.y),
        fork=(m.fork.x, m.fork.y),
        origin=m.origin,
        algorithm=m.algorithm,
        algorithm_version=m.algorithm_version,
        core_version=m.core_version,
        model_version=m.model_version,
    )


def _measurement_view(m: StoredMeasurement) -> MeasurementView:
    return MeasurementView(
        client_measurement_uuid=m.client_measurement_uuid,
        measured_at=m.measured_at,
        length_m=m.length_m,
        snout=Point(x=m.snout[0], y=m.snout[1]),
        fork=Point(x=m.fork[0], y=m.fork[1]),
        origin=m.origin,
        algorithm=m.algorithm,
        algorithm_version=m.algorithm_version,
        core_version=m.core_version,
        model_version=m.model_version,
    )


def _finite(value) -> bool:
    """Whether a JSON-shaped value holds no NaN or Infinity anywhere."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(v) for v in value)
    return True


def _view(detail: CaptureDetail, objects: TenantObjects | None) -> CaptureView:
    measurements = [_measurement_view(m) for m in detail.measurements]
    location = (
        None
        if detail.latitude is None
        else Location(
            latitude=detail.latitude,
            longitude=detail.longitude,
            horizontal_accuracy_m=detail.horizontal_accuracy_m,
            place_name=detail.place_name,
        )
    )
    return CaptureView(
        client_capture_uuid=detail.client_capture_uuid,
        capture_id=detail.capture_id,
        fish_id=detail.fish_id,
        imported_from=detail.imported_from,
        captured_at=detail.captured_at,
        device=DeviceInfo(
            install_id=detail.device_install_id,
            model=detail.device_model,
            info=detail.device_info,
        ),
        app_version=detail.app_version,
        core_version=detail.core_version,
        model_version=detail.model_version,
        location=location,
        orientation=detail.orientation,
        intrinsics=list(detail.intrinsics),
        camera_pose=None if detail.camera_pose is None else list(detail.camera_pose),
        depth=DepthGeometry(
            width=detail.depth_width,
            height=detail.depth_height,
            row_bytes=detail.depth_row_bytes,
        ),
        confidence=ConfidenceGeometry(
            width=detail.confidence_width,
            height=detail.confidence_height,
            row_bytes=detail.manifest.get("confidence", {}).get("row_bytes"),
        ),
        mask=(
            None
            if detail.mask_width is None
            else MaskGeometry(width=detail.mask_width, height=detail.mask_height)
        ),
        files=[
            FileView(
                role=f.role,
                sha256=f.sha256,
                size=f.size,
                content_type=f.content_type,
                url=objects.presign_get(f.key) if objects else None,
            )
            for f in detail.files
        ],
        measurements=measurements,
        current_measurement=measurements[-1] if measurements else None,
        manifest=detail.manifest,
        derived_place_name=detail.derived_place_name,
    )
