"""FishSense Mobile captures in the database (migration 0038).

Runs inside a tenant transaction (``app.tenant_id``), so RLS scopes every
read and write to that tenant; the queries also name the tenant explicitly,
the app layer's own line of defence. What the store checked before calling
in -- that every file arrived with the checksum and size declared -- is the
API's job (mobile_api.py); this module only keeps what it is given, once.

* :func:`commit_capture` -- the capture, its files, its device, its fish,
  and any measurement versions the phone has, in one transaction. Idempotent
  on ``(tenant, client_capture_uuid)``: the same capture again changes
  nothing; a different one under that uuid (other files, another uploader)
  is a :class:`CaptureConflict`, since a committed capture is immutable.
* :func:`append_device_measurement` -- a later version (a redone two-point
  selection), idempotent on the app's measurement uuid.
* :func:`list_mobile_captures`, :func:`mobile_capture` -- the reads.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text

from fishsense_services_api.phone_missions import assign_phone_mission
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "Appended",
    "CaptureConflict",
    "CaptureDetail",
    "CaptureFile",
    "CaptureSummary",
    "Committed",
    "DeviceMeasurement",
    "MobileCapture",
    "NoSuchCapture",
    "StoredMeasurement",
    "append_device_measurement",
    "committed_by",
    "commit_capture",
    "list_mobile_captures",
    "mobile_capture",
]

#: The capture's own image: its object is ``captures.raw_object_key`` and its
#: SHA-256 is ``captures.checksum``.
RGB_ROLE = "rgb_jpeg"
DEVICE_KIND = "mobile"


class CaptureConflict(Exception):
    """The uuid is taken by a different capture, or by another user's."""


class NoSuchCapture(LookupError):
    """No committed capture has that uuid in this tenant."""


@dataclass(frozen=True)
class CaptureFile:
    role: str
    bucket: str
    key: str
    #: Hex SHA-256.
    sha256: str
    size: int
    content_type: str


@dataclass(frozen=True)
class DeviceMeasurement:
    client_measurement_uuid: uuid.UUID
    measured_at: datetime
    length_m: float
    snout: tuple[float, float]
    fork: tuple[float, float]
    #: ``auto`` (the phone found the points), ``manual`` (the user did), or
    #: ``unknown`` (from before the app recorded which).
    origin: str
    algorithm: str | None
    algorithm_version: str | None
    core_version: str | None
    model_version: str | None


@dataclass(frozen=True)
class MobileCapture:
    client_capture_uuid: uuid.UUID
    uploaded_by: uuid.UUID
    captured_at: datetime
    device_install_id: str
    device_model: str | None
    device_info: str | None
    app_version: str | None
    core_version: str | None
    model_version: str | None
    latitude: float | None
    longitude: float | None
    horizontal_accuracy_m: float | None
    place_name: str | None
    orientation: str | None
    intrinsics: tuple[float, ...]
    camera_pose: tuple[float, ...] | None
    depth_width: int
    depth_height: int
    depth_row_bytes: int
    confidence_width: int
    confidence_height: int
    mask_width: int | None
    mask_height: int | None
    files: tuple[CaptureFile, ...]
    #: The commit request as received.
    manifest: dict = field(compare=False)

    @property
    def rgb(self) -> CaptureFile:
        return next(f for f in self.files if f.role == RGB_ROLE)


@dataclass(frozen=True)
class Committed:
    capture_id: uuid.UUID
    created: bool


@dataclass(frozen=True)
class StoredMeasurement:
    client_measurement_uuid: uuid.UUID
    measured_at: datetime
    length_m: float
    snout: tuple[float, float]
    fork: tuple[float, float]
    origin: str
    algorithm: str | None
    algorithm_version: str | None
    core_version: str | None
    model_version: str | None


@dataclass(frozen=True)
class Appended:
    created: bool
    #: The version as stored (on a re-send, the one already there).
    measurement: StoredMeasurement


@dataclass(frozen=True)
class CaptureSummary:
    client_capture_uuid: uuid.UUID
    captured_at: datetime
    latitude: float | None
    longitude: float | None
    place_name: str | None
    length_m: float | None
    rgb_key: str
    capture_id: uuid.UUID
    #: The name the server derived from the coordinates (0041), if any.
    derived_place_name: str | None = None

    @property
    def cursor(self) -> tuple[datetime, uuid.UUID]:
        """Where the next page starts (pass as ``before``)."""
        return (self.captured_at, self.capture_id)


@dataclass(frozen=True)
class CaptureDetail:
    capture_id: uuid.UUID
    client_capture_uuid: uuid.UUID
    uploaded_by: uuid.UUID
    captured_at: datetime
    device_install_id: str
    device_model: str | None
    device_info: str | None
    app_version: str | None
    core_version: str | None
    model_version: str | None
    latitude: float | None
    longitude: float | None
    horizontal_accuracy_m: float | None
    place_name: str | None
    orientation: str | None
    intrinsics: tuple[float, ...]
    camera_pose: tuple[float, ...] | None
    depth_width: int
    depth_height: int
    depth_row_bytes: int
    confidence_width: int
    confidence_height: int
    mask_width: int | None
    mask_height: int | None
    manifest: dict
    files: tuple[CaptureFile, ...]
    #: Oldest first.
    measurements: tuple[StoredMeasurement, ...]
    #: The name the server derived from the coordinates (0041), if any.
    derived_place_name: str | None = None

    @property
    def current_measurement(self) -> StoredMeasurement | None:
        return self.measurements[-1] if self.measurements else None


async def commit_capture(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture: MobileCapture,
    measurements: Sequence[DeviceMeasurement],
) -> Committed:
    # A phone's retry can race its own first attempt: one at a time per uuid,
    # so the loser finds the capture kept rather than colliding with it.
    await _lock(conn, tenant_id, capture.client_capture_uuid)
    existing = await _committed(conn, tenant_id, capture.client_capture_uuid)
    if existing is not None:
        if existing.uploaded_by != capture.uploaded_by:
            raise CaptureConflict("this capture was committed by another user")
        if existing.files != {
            (f.role, f.sha256, f.size) for f in capture.files
        } or _as_kept(existing.manifest) != _as_kept(capture.manifest):
            raise CaptureConflict("a committed capture is immutable")
        for measurement in sorted(measurements, key=_measured_order):
            await _append(conn, tenant_id, existing.capture_id, measurement)
        return Committed(capture_id=existing.capture_id, created=False)

    # One photo, one capture: two uuids racing with the same photo wait here
    # (always after the uuid lock, so two commits can't deadlock).
    await _lock(conn, tenant_id, f"photo:{capture.rgb.sha256}")
    duplicate = await _canonical_photo(conn, tenant_id, capture.rgb.sha256)
    if duplicate is not None:
        raise CaptureConflict(f"this photo is already capture {duplicate}")

    device_id = await _device(conn, tenant_id, capture.device_install_id)
    capture_id = (
        await conn.execute(
            text("""
                INSERT INTO captures (tenant_id, device_id, raw_object_key,
                    captured_at, checksum, checksum_algorithm, is_canonical,
                    client_capture_uuid, uploaded_by, latitude, longitude,
                    horizontal_accuracy_m)
                VALUES (:tenant_id, :device_id, :key, :captured_at, :checksum,
                    'sha256', true, :client_uuid, :uploaded_by, :latitude,
                    :longitude, :accuracy)
                RETURNING id
                """),
            {
                "tenant_id": tenant_id,
                "device_id": device_id,
                "key": capture.rgb.key,
                "captured_at": capture.captured_at,
                "checksum": capture.rgb.sha256,
                "client_uuid": capture.client_capture_uuid,
                "uploaded_by": capture.uploaded_by,
                "latitude": capture.latitude,
                "longitude": capture.longitude,
                "accuracy": capture.horizontal_accuracy_m,
            },
        )
    ).scalar_one()
    fish_id = (
        await conn.execute(
            text("INSERT INTO fish (tenant_id) VALUES (:t) RETURNING id"),
            {"t": tenant_id},
        )
    ).scalar_one()
    await conn.execute(
        text("""
            INSERT INTO mobile_captures (tenant_id, capture_id, fish_id,
                device_model, device_info, app_version, core_version,
                model_version, place_name, orientation, intrinsics, camera_pose,
                depth_width, depth_height, depth_row_bytes, confidence_width,
                confidence_height, mask_width, mask_height, manifest)
            VALUES (:tenant_id, :capture_id, :fish_id, :device_model,
                :device_info, :app_version, :core_version, :model_version,
                :place_name, :orientation, :intrinsics, :camera_pose,
                :depth_width, :depth_height, :depth_row_bytes,
                :confidence_width, :confidence_height, :mask_width,
                :mask_height, CAST(:manifest AS jsonb))
            """),
        {
            "tenant_id": tenant_id,
            "capture_id": capture_id,
            "fish_id": fish_id,
            "device_model": capture.device_model,
            "device_info": capture.device_info,
            "app_version": capture.app_version,
            "core_version": capture.core_version,
            "model_version": capture.model_version,
            "place_name": capture.place_name,
            "orientation": capture.orientation,
            "intrinsics": list(capture.intrinsics),
            "camera_pose": (
                None if capture.camera_pose is None else list(capture.camera_pose)
            ),
            "depth_width": capture.depth_width,
            "depth_height": capture.depth_height,
            "depth_row_bytes": capture.depth_row_bytes,
            "confidence_width": capture.confidence_width,
            "confidence_height": capture.confidence_height,
            "mask_width": capture.mask_width,
            "mask_height": capture.mask_height,
            "manifest": json.dumps(capture.manifest),
        },
    )
    for f in capture.files:
        await conn.execute(
            text("""
                INSERT INTO capture_objects (tenant_id, capture_id, role, bucket,
                    object_key, sha256, size_bytes, content_type)
                VALUES (:tenant_id, :capture_id, :role, :bucket, :key, :sha256,
                    :size, :content_type)
                """),
            {
                "tenant_id": tenant_id,
                "capture_id": capture_id,
                "role": f.role,
                "bucket": f.bucket,
                "key": f.key,
                "sha256": f.sha256,
                "size": f.size,
                "content_type": f.content_type,
            },
        )
    for measurement in sorted(measurements, key=_measured_order):
        await _append(conn, tenant_id, capture_id, measurement)
    await assign_phone_mission(
        conn,
        tenant_id,
        capture_id=capture_id,
        device_id=device_id,
        captured_at=capture.captured_at,
        latitude=capture.latitude,
        longitude=capture.longitude,
        place=capture.place_name,
        time_zone=capture.manifest.get("time_zone"),
    )
    return Committed(capture_id=capture_id, created=True)


async def append_device_measurement(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    client_capture_uuid: uuid.UUID,
    uploaded_by: uuid.UUID,
    measurement: DeviceMeasurement,
) -> Appended:
    """Append a measurement version. Re-sending it is a no-op
    (``created=False``); its uuid on another capture, or with other values,
    is a :class:`CaptureConflict`."""
    await _lock(conn, tenant_id, client_capture_uuid)
    existing = await _committed(conn, tenant_id, client_capture_uuid)
    if existing is None:
        raise NoSuchCapture(str(client_capture_uuid))
    if existing.uploaded_by != uploaded_by:
        raise CaptureConflict("only the user who took a capture may measure it")
    return await _append(conn, tenant_id, existing.capture_id, measurement)


async def committed_by(
    conn: AsyncConnection, tenant_id: uuid.UUID, client_capture_uuid: uuid.UUID
) -> uuid.UUID | None:
    """Who committed this capture, or None if it isn't committed."""
    return (
        await conn.execute(
            text("""
                SELECT uploaded_by FROM captures
                WHERE tenant_id = :tenant_id AND client_capture_uuid = :client_uuid
                """),
            {"tenant_id": tenant_id, "client_uuid": client_capture_uuid},
        )
    ).scalar_one_or_none()


async def list_mobile_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    limit: int,
    before: tuple[datetime, uuid.UUID] | None = None,
) -> list[CaptureSummary]:
    """Newest first; ``before`` is the previous page's last ``cursor``."""
    rows = await conn.execute(
        text(f"""
            SELECT c.id AS capture_id, c.client_capture_uuid, c.captured_at,
                c.latitude, c.longitude, mc.place_name, c.raw_object_key,
                cm.length_m, pn.place_name AS derived_place_name
            FROM captures c
            JOIN mobile_captures mc
              ON mc.tenant_id = c.tenant_id AND mc.capture_id = c.id
            LEFT JOIN current_measurements cm
              ON cm.tenant_id = c.tenant_id AND cm.capture_id = c.id
             AND cm.fish_id = mc.fish_id AND cm.source = 'device'
            {_DERIVED_PLACE_NAME}
            WHERE c.tenant_id = :tenant_id
              AND (CAST(:before_at AS timestamptz) IS NULL
                   OR (c.captured_at, c.id) < (:before_at, :before_id))
            ORDER BY c.captured_at DESC, c.id DESC
            LIMIT :limit
            """),
        {
            "tenant_id": tenant_id,
            "before_at": before[0] if before else None,
            "before_id": before[1] if before else None,
            "limit": limit,
        },
    )
    return [
        CaptureSummary(
            client_capture_uuid=row.client_capture_uuid,
            captured_at=row.captured_at,
            latitude=row.latitude,
            longitude=row.longitude,
            place_name=row.place_name,
            length_m=row.length_m,
            rgb_key=row.raw_object_key,
            capture_id=row.capture_id,
            derived_place_name=row.derived_place_name,
        )
        for row in rows
    ]


async def mobile_capture(
    conn: AsyncConnection, tenant_id: uuid.UUID, client_capture_uuid: uuid.UUID
) -> CaptureDetail | None:
    row = (
        await conn.execute(
            text(f"""
                SELECT c.id, c.client_capture_uuid, c.uploaded_by, c.captured_at,
                    d.serial, c.latitude, c.longitude, c.horizontal_accuracy_m,
                    mc.*, pn.place_name AS derived_place_name
                FROM captures c
                JOIN mobile_captures mc
                  ON mc.tenant_id = c.tenant_id AND mc.capture_id = c.id
                JOIN devices d ON d.tenant_id = c.tenant_id AND d.id = c.device_id
                {_DERIVED_PLACE_NAME}
                WHERE c.tenant_id = :tenant_id
                  AND c.client_capture_uuid = :client_uuid
                """),
            {"tenant_id": tenant_id, "client_uuid": client_capture_uuid},
        )
    ).one_or_none()
    if row is None:
        return None
    files = await conn.execute(
        text("""
            SELECT role, bucket, object_key, sha256, size_bytes, content_type
            FROM capture_objects
            WHERE tenant_id = :tenant_id AND capture_id = :capture_id
            ORDER BY role
            """),
        {"tenant_id": tenant_id, "capture_id": row.id},
    )
    measurements = await conn.execute(
        text(f"""
            SELECT {_VERSION_COLUMNS}
            {_VERSION_FROM}
            WHERE m.tenant_id = :tenant_id AND m.capture_id = :capture_id
              AND m.source = 'device'
            ORDER BY m.measured_at, m.seq
            """),
        {"tenant_id": tenant_id, "capture_id": row.id},
    )
    return CaptureDetail(
        capture_id=row.id,
        client_capture_uuid=row.client_capture_uuid,
        uploaded_by=row.uploaded_by,
        captured_at=row.captured_at,
        device_install_id=row.serial,
        device_model=row.device_model,
        device_info=row.device_info,
        app_version=row.app_version,
        core_version=row.core_version,
        model_version=row.model_version,
        latitude=row.latitude,
        longitude=row.longitude,
        horizontal_accuracy_m=row.horizontal_accuracy_m,
        place_name=row.place_name,
        orientation=row.orientation,
        derived_place_name=row.derived_place_name,
        intrinsics=tuple(row.intrinsics),
        camera_pose=None if row.camera_pose is None else tuple(row.camera_pose),
        depth_width=row.depth_width,
        depth_height=row.depth_height,
        depth_row_bytes=row.depth_row_bytes,
        confidence_width=row.confidence_width,
        confidence_height=row.confidence_height,
        mask_width=row.mask_width,
        mask_height=row.mask_height,
        manifest=row.manifest,
        files=tuple(
            CaptureFile(
                role=f.role,
                bucket=f.bucket,
                key=f.object_key,
                sha256=f.sha256,
                size=f.size_bytes,
                content_type=f.content_type,
            )
            for f in files
        ),
        measurements=tuple(_stored(m) for m in measurements),
    )


#: A device measurement version: the measurement and its points.
_VERSION_COLUMNS = """p.client_measurement_uuid, m.measured_at, m.length_m,
                p.snout_x, p.snout_y, p.fork_x, p.fork_y, p.origin, m.algorithm,
                m.algorithm_version, m.core_version, m.model_version"""
_VERSION_FROM = """FROM measurements m
            JOIN device_measurement_points p
              ON p.tenant_id = m.tenant_id AND p.measurement_id = m.id"""


def _stored(row) -> StoredMeasurement:
    return StoredMeasurement(
        client_measurement_uuid=row.client_measurement_uuid,
        measured_at=row.measured_at,
        length_m=row.length_m,
        snout=(row.snout_x, row.snout_y),
        fork=(row.fork_x, row.fork_y),
        origin=row.origin,
        algorithm=row.algorithm,
        algorithm_version=row.algorithm_version,
        core_version=row.core_version,
        model_version=row.model_version,
    )


def _as_received(m: DeviceMeasurement) -> StoredMeasurement:
    return StoredMeasurement(
        client_measurement_uuid=m.client_measurement_uuid,
        measured_at=m.measured_at,
        length_m=m.length_m,
        snout=m.snout,
        fork=m.fork,
        origin=m.origin,
        algorithm=m.algorithm,
        algorithm_version=m.algorithm_version,
        core_version=m.core_version,
        model_version=m.model_version,
    )


def _measured_order(m: DeviceMeasurement):
    return m.measured_at


def _as_kept(manifest: dict) -> dict:
    """What makes a capture this capture: everything but its measurement
    versions, which a retry may carry more of (they are appended)."""
    return {k: v for k, v in manifest.items() if k != "measurements"}


async def _lock(conn: AsyncConnection, tenant_id: uuid.UUID, subject) -> None:
    """Serialize writes to one capture (a uuid) or one photo until this
    transaction ends."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"mobile-capture:{tenant_id}:{subject}"},
    )


@dataclass(frozen=True)
class _Existing:
    capture_id: uuid.UUID
    uploaded_by: uuid.UUID
    files: set[tuple[str, str, int]]
    manifest: dict


async def _committed(
    conn: AsyncConnection, tenant_id: uuid.UUID, client_capture_uuid: uuid.UUID
) -> _Existing | None:
    row = (
        await conn.execute(
            text("""
                SELECT c.id, c.uploaded_by, mc.manifest FROM captures c
                JOIN mobile_captures mc
                  ON mc.tenant_id = c.tenant_id AND mc.capture_id = c.id
                WHERE c.tenant_id = :tenant_id
                  AND c.client_capture_uuid = :client_uuid
                """),
            {"tenant_id": tenant_id, "client_uuid": client_capture_uuid},
        )
    ).one_or_none()
    if row is None:
        return None
    files = await conn.execute(
        text("""
            SELECT role, sha256, size_bytes FROM capture_objects
            WHERE tenant_id = :tenant_id AND capture_id = :capture_id
            """),
        {"tenant_id": tenant_id, "capture_id": row.id},
    )
    return _Existing(
        capture_id=row.id,
        uploaded_by=row.uploaded_by,
        files={(f.role, f.sha256, f.size_bytes) for f in files},
        manifest=row.manifest,
    )


async def _canonical_photo(
    conn: AsyncConnection, tenant_id: uuid.UUID, sha256: str
) -> uuid.UUID | None:
    """The capture (by its client uuid) already holding this exact photo."""
    return (
        await conn.execute(
            text("""
                SELECT client_capture_uuid FROM captures
                WHERE tenant_id = :tenant_id AND is_canonical
                  AND checksum_algorithm = 'sha256' AND checksum = :sha256
                """),
            {"tenant_id": tenant_id, "sha256": sha256},
        )
    ).scalar_one_or_none()


async def _device(conn: AsyncConnection, tenant_id: uuid.UUID, install_id: str):
    await conn.execute(
        text("""
            INSERT INTO devices (tenant_id, kind, serial)
            VALUES (:tenant_id, :kind, :serial)
            ON CONFLICT (tenant_id, serial) DO NOTHING
            """),
        {"tenant_id": tenant_id, "kind": DEVICE_KIND, "serial": install_id},
    )
    device = (
        await conn.execute(
            text("""
                SELECT id, kind FROM devices
                WHERE tenant_id = :tenant_id AND serial = :serial
                """),
            {"tenant_id": tenant_id, "serial": install_id},
        )
    ).one()
    if device.kind != DEVICE_KIND:
        raise CaptureConflict(f"device {install_id!r} is not a mobile device")
    return device.id


async def _append(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    capture_id: uuid.UUID,
    m: DeviceMeasurement,
) -> Appended:
    seen = (
        await conn.execute(
            text(f"""
                SELECT m.capture_id, {_VERSION_COLUMNS}
                {_VERSION_FROM}
                WHERE p.tenant_id = :tenant_id AND p.client_measurement_uuid = :key
                """),
            {"tenant_id": tenant_id, "key": m.client_measurement_uuid},
        )
    ).one_or_none()
    if seen is not None:
        stored = _stored(seen)
        if seen.capture_id != capture_id:
            raise CaptureConflict(
                f"measurement {m.client_measurement_uuid} is another capture's"
            )
        if stored != _as_received(m):
            raise CaptureConflict(
                f"measurement {m.client_measurement_uuid} was sent with other values"
            )
        return Appended(created=False, measurement=stored)
    fish_id = (
        await conn.execute(
            text("""
                SELECT fish_id FROM mobile_captures
                WHERE tenant_id = :tenant_id AND capture_id = :capture_id
                """),
            {"tenant_id": tenant_id, "capture_id": capture_id},
        )
    ).scalar_one()
    measurement_id = (
        await conn.execute(
            text("""
                INSERT INTO measurements (tenant_id, capture_id, fish_id, source,
                    length_m, algorithm, algorithm_version, core_version,
                    model_version, measured_at)
                VALUES (:tenant_id, :capture_id, :fish_id, 'device', :length_m,
                    :algorithm, :algorithm_version, :core_version, :model_version,
                    :measured_at)
                RETURNING id
                """),
            {
                "tenant_id": tenant_id,
                "capture_id": capture_id,
                "fish_id": fish_id,
                "length_m": m.length_m,
                "algorithm": m.algorithm,
                "algorithm_version": m.algorithm_version,
                "core_version": m.core_version,
                "model_version": m.model_version,
                "measured_at": m.measured_at,
            },
        )
    ).scalar_one()
    await conn.execute(
        text("""
            INSERT INTO device_measurement_points (tenant_id, measurement_id,
                client_measurement_uuid, origin, snout_x, snout_y, fork_x, fork_y)
            VALUES (:tenant_id, :measurement_id, :key, :origin, :snout_x,
                :snout_y, :fork_x, :fork_y)
            """),
        {
            "tenant_id": tenant_id,
            "measurement_id": measurement_id,
            "key": m.client_measurement_uuid,
            "origin": m.origin,
            "snout_x": m.snout[0],
            "snout_y": m.snout[1],
            "fork_x": m.fork[0],
            "fork_y": m.fork[1],
        },
    )
    return Appended(created=True, measurement=_as_received(m))


#: A capture's latest derived place name (0041), as `pn`.
_DERIVED_PLACE_NAME = """LEFT JOIN LATERAL (
                SELECT place_name FROM capture_place_names
                WHERE tenant_id = c.tenant_id AND capture_id = c.id
                ORDER BY derived_at DESC LIMIT 1
            ) pn ON true"""
