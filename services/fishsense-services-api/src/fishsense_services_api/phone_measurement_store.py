"""The phone measurement's database side (0087; the owner, 2026-11-09).

The server measures a phone capture that was never measured, as FishSense
Mobile measures it (the owner's rule, 2026-10-21): one with its JPEG and
LiDAR depth stored, no measurement the phone synced, and no server result
yet. The result is unvalidated (the owner, 2026-11-09), so it is written to
``phone_measurements``, never ``measurements``.

* `owed_missions` / `owed_captures`: the work, a mission at a time, a page
  at a time (by capture id), each capture with its files' keys, its depth's
  geometry and its K;
* `record_phone_measurements`: each result appended, its mask named in the
  row. A capture that is not one of the tenant's phone captures is refused,
  as is a mask stored under another capture's key: the processor runs on
  machines we don't own.

Re-measuring a capture a person flagged is not here yet: the web fix that
flags one is the next step.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "OwedMission",
    "OwedPhoneCapture",
    "PHONE_STATUSES",
    "PhoneMeasurementCatalog",
    "PhoneMeasurementMask",
    "PhoneMeasurementResult",
    "owed_captures",
    "owed_missions",
    "record_phone_measurements",
]

#: The contract's (`fishsense_services_contracts.phone_measurement`), repeated:
#: the API image has no contracts package. The orchestrator pins them equal.
PHONE_STATUSES = ("measured", "no_fish", "headtail_failed", "depth_failed",
                  "image_unavailable", "decode_failed")  # fmt: skip

#: The tenant's phone captures owed a measurement, with their mission, files,
#: depth geometry and K.
_OWED = """
    SELECT c.id AS capture_id, c.mission_id, rgb.bucket, rgb.object_key AS rgb_key,
           depth.object_key AS depth_key, depth.bucket AS depth_bucket,
           mc.depth_width, mc.depth_height, mc.depth_row_bytes, mc.intrinsics
    FROM captures c
    JOIN mobile_captures mc ON mc.tenant_id = c.tenant_id AND mc.capture_id = c.id
    JOIN capture_objects rgb ON rgb.tenant_id = c.tenant_id
         AND rgb.capture_id = c.id AND rgb.role = 'rgb_jpeg'
    JOIN capture_objects depth ON depth.tenant_id = c.tenant_id
         AND depth.capture_id = c.id AND depth.role = 'depth_f32'
    WHERE c.tenant_id = :t
      AND NOT EXISTS (SELECT 1 FROM measurements m
                      WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                        AND m.source = 'device')
      AND NOT EXISTS (SELECT 1 FROM phone_measurements p
                      WHERE p.tenant_id = c.tenant_id AND p.capture_id = c.id)
"""


@dataclass(frozen=True)
class OwedMission:
    mission_id: uuid.UUID
    number: int
    captures: int


@dataclass(frozen=True)
class OwedPhoneCapture:
    capture_id: uuid.UUID
    bucket: str
    rgb_key: str
    depth_key: str
    depth_width: int
    depth_height: int
    depth_row_bytes: int
    #: Row-major K, the frame's pixels.
    intrinsics: tuple[float, ...]


@dataclass(frozen=True)
class PhoneMeasurementMask:
    """A mask the orchestrator read back and checked."""

    bucket: str
    key: str
    sha256: str
    size: int
    width: int
    height: int


@dataclass(frozen=True)
class PhoneMeasurementResult:
    capture_id: uuid.UUID
    status: str
    length_m: float | None
    snout_x: float | None
    snout_y: float | None
    fork_x: float | None
    fork_y: float | None
    snout_depth_m: float | None
    fork_depth_m: float | None
    model_id: str | None
    core_version: str | None
    algorithm: str
    error: str | None
    mask: PhoneMeasurementMask | None = None


async def owed_missions(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[OwedMission]:
    """Each mission with phone captures owed a measurement, and how many."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.number, count(*) AS n
            FROM ({_OWED}) o JOIN missions d ON d.tenant_id = :t AND d.id = o.mission_id
            GROUP BY d.id, d.number ORDER BY d.number
            """),
        {"t": tenant_id},
    )
    return [OwedMission(r.id, r.number, r.n) for r in rows]


async def owed_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    after: uuid.UUID | None = None,
    limit: int = 100,
) -> list[OwedPhoneCapture]:
    """A page of one mission's captures owed a measurement, by capture id."""
    rows = await conn.execute(
        text(f"""
            SELECT * FROM ({_OWED}) o
            WHERE o.mission_id = :d
              AND (CAST(:after AS uuid) IS NULL OR o.capture_id > CAST(:after AS uuid))
            ORDER BY o.capture_id LIMIT :limit
            """),
        {"t": tenant_id, "d": mission_id, "after": after, "limit": limit},
    )
    out = []
    for r in rows:
        if r.depth_bucket != r.bucket:
            raise ValueError(f"capture {r.capture_id}'s files are in two buckets")
        out.append(OwedPhoneCapture(
            capture_id=r.capture_id, bucket=r.bucket, rgb_key=r.rgb_key,
            depth_key=r.depth_key, depth_width=r.depth_width,
            depth_height=r.depth_height, depth_row_bytes=r.depth_row_bytes,
            intrinsics=tuple(float(v) for v in r.intrinsics),
        ))  # fmt: skip
    return out


async def record_phone_measurements(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    results: Sequence[PhoneMeasurementResult],
) -> int:
    """Append each result; how many were written."""
    for r in results:
        is_phone = (
            await conn.execute(
                text(
                    "SELECT 1 FROM mobile_captures WHERE tenant_id = :t AND capture_id = :c"
                ),
                {"t": tenant_id, "c": r.capture_id},
            )
        ).scalar_one_or_none()
        if is_phone is None:
            raise ValueError(
                f"capture {r.capture_id} is not one of the tenant's phone captures"
            )
        m = r.mask
        if m is not None and not m.key.startswith(f"masks/phone/{r.capture_id}/"):
            raise ValueError(f"mask {m.key} is not capture {r.capture_id}'s")
        await conn.execute(
            text("""
                INSERT INTO phone_measurements (tenant_id, capture_id, status, length_m,
                    snout_x, snout_y, fork_x, fork_y, snout_depth_m, fork_depth_m,
                    mask_bucket, mask_key, mask_sha256, mask_size_bytes, mask_width,
                    mask_height, model_id, core_version, algorithm, error)
                VALUES (:t, :c, :status, :length, :sx, :sy, :fx, :fy, :sd, :fd,
                    :mb, :mk, :ms, :mz, :mw, :mh, :model, :core, :algorithm, :error)
                """),
            {"t": tenant_id, "c": r.capture_id, "status": r.status, "length": r.length_m,
             "sx": r.snout_x, "sy": r.snout_y, "fx": r.fork_x, "fy": r.fork_y,
             "sd": r.snout_depth_m, "fd": r.fork_depth_m,
             "mb": None if m is None else m.bucket, "mk": None if m is None else m.key,
             "ms": None if m is None else m.sha256, "mz": None if m is None else m.size,
             "mw": None if m is None else m.width, "mh": None if m is None else m.height,
             "model": r.model_id, "core": r.core_version, "algorithm": r.algorithm,
             "error": r.error},
        )  # fmt: skip
    return len(results)


class PhoneMeasurementCatalog(ServicePrincipal):
    """The phone measurement's database side, as the orchestrator's service
    principal."""

    async def owed_missions(self, tenant_id):
        async with self._tenant(tenant_id) as conn:
            return await owed_missions(conn, tenant_id)

    async def owed_captures(self, tenant_id, mission_id, *, after=None, limit=100):
        async with self._tenant(tenant_id) as conn:
            return await owed_captures(
                conn, tenant_id, mission_id, after=after, limit=limit
            )

    async def record(self, tenant_id, results):
        async with self._tenant(tenant_id) as conn:
            return await record_phone_measurements(conn, tenant_id, results)
