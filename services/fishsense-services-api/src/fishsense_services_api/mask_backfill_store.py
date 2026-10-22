"""The mask backfill's database side (2026-10-22).

Masks have been named against their predictions since 0059 (2026-10-21);
predictions made before then kept theirs only as numbers. The backfill finds
each current prediction that kept a mask (``predicted`` or
``headtail_failed``, with its area and crop) and names none, with what the
processor needs to regenerate it: the dot(s) it was made from, the image (the
automatic track's render version and camera; the head/tail stage's capture
checksum, for its stage-5.1 JPEG), and what it recorded, to check the
regeneration against. It writes only `prediction_masks` rows naming existing
predictions -- never a prediction.

* `unmasked_missions` / `unmasked_predictions`: the work, a mission at a
  time, a page at a time (by `seq`);
* `record_backfilled_masks`: the masks the orchestrator read back and
  checked, each named against its prediction -- a mask for another capture
  is refused, a prediction already naming one is left as it is.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.prediction_masks_store import PredictionMask
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "MASK_BACKFILL_SOURCES",
    "MaskBackfillCatalog",
    "UnmaskedMission",
    "UnmaskedPrediction",
    "record_backfilled_masks",
    "unmasked_missions",
    "unmasked_predictions",
]

MASK_BACKFILL_SOURCES = ("automatic", "headtail")
_KEPT = "('predicted', 'headtail_failed')"

#: Per source: the current predictions that kept a mask and name none, with
#: their capture's mission. Each row: what the processor needs.
_UNMASKED = {
    "automatic": f"""
        SELECT p.id AS prediction_id, p.seq, p.capture_id, c.mission_id,
               p.laser_x, p.laser_y, NULL::uuid AS laser_label_id,
               p.mask_area_px, p.crop_x, p.crop_y,
               p.head_x, p.head_y, p.tail_x, p.tail_y, p.checkpoint,
               p.jpeg_render_version AS render_version,
               p.jpeg_camera_calibration_id AS camera_calibration_id,
               c.checksum, c.v1_id IS NOT NULL AS from_v1
        FROM current_automatic_head_tail_predictions p
        JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
        WHERE p.tenant_id = :t AND p.status IN {_KEPT}
          AND p.mask_area_px IS NOT NULL AND p.crop_x IS NOT NULL
          AND p.crop_y IS NOT NULL AND p.laser_x IS NOT NULL
          AND p.jpeg_render_version IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM prediction_masks m
                          WHERE m.tenant_id = p.tenant_id
                            AND m.automatic_head_tail_prediction_id = p.id)
        """,
    "headtail": f"""
        SELECT p.id AS prediction_id, p.seq, p.capture_id, c.mission_id,
               l.x AS laser_x, l.y AS laser_y, p.laser_label_id,
               p.mask_area_px, p.crop_x, p.crop_y,
               p.head_x, p.head_y, p.tail_x, p.tail_y, p.checkpoint,
               NULL::int AS render_version, NULL::uuid AS camera_calibration_id,
               c.checksum, c.v1_id IS NOT NULL AS from_v1
        FROM current_head_tail_predictions p
        JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
        JOIN laser_labels l ON l.tenant_id = p.tenant_id AND l.id = p.laser_label_id
        WHERE p.tenant_id = :t AND p.status IN {_KEPT}
          AND p.mask_area_px IS NOT NULL AND p.crop_x IS NOT NULL
          AND p.crop_y IS NOT NULL AND l.x IS NOT NULL AND l.y IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM prediction_masks m
                          WHERE m.tenant_id = p.tenant_id
                            AND m.head_tail_prediction_id = p.id)
        """,
}
_COLUMN = {
    "automatic": "automatic_head_tail_prediction_id",
    "headtail": "head_tail_prediction_id",
}
_TABLE = {
    "automatic": "automatic_head_tail_predictions",
    "headtail": "head_tail_predictions",
}


@dataclass(frozen=True)
class UnmaskedMission:
    mission_id: uuid.UUID
    number: int
    predictions: int


@dataclass(frozen=True)
class UnmaskedPrediction:
    prediction_id: uuid.UUID
    seq: int
    capture_id: uuid.UUID
    #: The dot the prediction was made from, rectified px.
    dot: tuple[float, float]
    #: The head/tail stage's: the labelled dot's label.
    laser_label_id: uuid.UUID | None
    mask_area_px: int
    crop_x: int
    crop_y: int
    head_x: float | None
    head_y: float | None
    tail_x: float | None
    tail_y: float | None
    checkpoint: str | None
    #: The automatic track's: the render it was found in.
    render_version: int | None
    camera_calibration_id: uuid.UUID | None
    #: The head/tail stage's: where its stage-5.1 JPEG is.
    checksum: str
    from_v1: bool


def _source(source: str) -> str:
    if source not in MASK_BACKFILL_SOURCES:
        raise ValueError(f"no mask source {source!r}")
    return source


async def unmasked_missions(
    conn: AsyncConnection, tenant_id: uuid.UUID, source: str
) -> list[UnmaskedMission]:
    """Each mission with predictions owed a mask, and how many, by number."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.number, count(*) AS n
            FROM ({_UNMASKED[_source(source)]}) u
            JOIN missions d ON d.tenant_id = :t AND d.id = u.mission_id
            GROUP BY d.id, d.number ORDER BY d.number
            """),
        {"t": tenant_id},
    )
    return [UnmaskedMission(r.id, r.number, r.n) for r in rows]


async def unmasked_predictions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    source: str,
    mission_id: uuid.UUID,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> list[UnmaskedPrediction]:
    """A page of one mission's predictions owed a mask, in `seq` order."""
    rows = await conn.execute(
        text(f"""
            SELECT * FROM ({_UNMASKED[_source(source)]}) u
            WHERE u.mission_id = :d AND u.seq > :after
            ORDER BY u.seq LIMIT :limit
            """),
        {"t": tenant_id, "d": mission_id, "after": after_seq, "limit": limit},
    )
    return [
        UnmaskedPrediction(
            prediction_id=r.prediction_id, seq=r.seq, capture_id=r.capture_id,
            dot=(float(r.laser_x), float(r.laser_y)), laser_label_id=r.laser_label_id,
            mask_area_px=r.mask_area_px, crop_x=r.crop_x, crop_y=r.crop_y,
            head_x=r.head_x, head_y=r.head_y, tail_x=r.tail_x, tail_y=r.tail_y,
            checkpoint=r.checkpoint, render_version=r.render_version,
            camera_calibration_id=r.camera_calibration_id, checksum=r.checksum,
            from_v1=r.from_v1,
        )  # fmt: skip
        for r in rows
    ]


async def record_backfilled_masks(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    source: str,
    prediction_ids: Sequence[uuid.UUID],
    masks: Sequence[PredictionMask],
) -> int:
    """Name each checked mask against its prediction; how many were named.
    A prediction that is not the tenant's, or whose capture is not the
    mask's, is refused (ValueError): the processor runs on machines we don't
    own. One already naming a mask is left as it is (a retried persist)."""
    if len(prediction_ids) != len(masks):
        raise ValueError("a mask per prediction")
    column, table = _COLUMN[_source(source)], _TABLE[source]
    written = 0
    for prediction, m in zip(prediction_ids, masks, strict=True):
        capture = (
            await conn.execute(
                text(
                    f"SELECT capture_id FROM {table} WHERE tenant_id = :t AND id = :p"
                ),
                {"t": tenant_id, "p": prediction},
            )
        ).scalar_one_or_none()
        if capture is None:
            raise ValueError(f"no {source} prediction {prediction}")
        if capture != m.capture_id:
            raise ValueError(
                f"a mask of capture {m.capture_id} for prediction {prediction} "
                f"of capture {capture}"
            )
        result = await conn.execute(
            text(f"""
                INSERT INTO prediction_masks (tenant_id, capture_id, {column},
                    bucket, object_key, sha256, size_bytes, content_type, width,
                    height)
                SELECT :t, :capture, :prediction, :bucket, :key, :sha256, :size,
                    :content_type, :width, :height
                WHERE NOT EXISTS (SELECT 1 FROM prediction_masks x
                                  WHERE x.tenant_id = :t AND x.{column} = :prediction)
                ON CONFLICT (bucket, object_key) DO NOTHING
                """),
            {"t": tenant_id, "capture": m.capture_id, "prediction": prediction,
             "bucket": m.bucket, "key": m.key, "sha256": m.sha256, "size": m.size,
             "content_type": m.content_type, "width": m.width, "height": m.height},
        )  # fmt: skip
        written += result.rowcount
    return written


class MaskBackfillCatalog(ServicePrincipal):
    """The mask backfill's database side, as the orchestrator's service
    principal."""

    async def unmasked_missions(self, tenant_id, source):
        async with self._tenant(tenant_id) as conn:
            return await unmasked_missions(conn, tenant_id, source)

    async def unmasked_predictions(self, tenant_id, source, mission_id, *,
                                   after_seq=0, limit=200):  # fmt: skip
        async with self._tenant(tenant_id) as conn:
            return await unmasked_predictions(
                conn, tenant_id, source, mission_id, after_seq=after_seq, limit=limit
            )

    async def record_backfilled_masks(self, tenant_id, source, prediction_ids, masks):
        async with self._tenant(tenant_id) as conn:
            return await record_backfilled_masks(
                conn, tenant_id, source, prediction_ids, masks
            )
