"""SAM masks named against their predictions (0058, 2026-10-21).

A kept mask is stored in the tenant's bucket the way FishSense Mobile stores
its ``mask_u8`` (gzip'd; `contracts.masks`, which this package must not
import: its image has no contracts). The orchestrator checks the stored object
first; this writes the row naming it, in the same transaction as the
prediction it belongs to (`persist_automatic_head_tails`,
`persist_headtail_predictions`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["PredictionMask", "insert_prediction_masks"]

_COLUMNS = {
    "automatic": "automatic_head_tail_prediction_id",
    "headtail": "head_tail_prediction_id",
}


@dataclass(frozen=True)
class PredictionMask:
    """A stored mask, as the orchestrator checked it, for one capture's
    prediction in the batch being written."""

    capture_id: uuid.UUID
    bucket: str
    key: str
    sha256: str
    size: int
    content_type: str
    width: int
    height: int


async def insert_prediction_masks(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    source: str,
    prediction_ids: Mapping[uuid.UUID, uuid.UUID],
    masks: Sequence[PredictionMask],
) -> int:
    """Name each mask against the prediction just written for its capture
    (`prediction_ids`: capture -> prediction). A mask for a capture with no
    prediction in the batch is refused (ValueError): it would name nothing."""
    column = _COLUMNS[source]
    for m in masks:
        prediction = prediction_ids.get(m.capture_id)
        if prediction is None:
            raise ValueError(f"a mask for capture {m.capture_id}, not in the batch")
        await conn.execute(
            text(f"""
                INSERT INTO prediction_masks (tenant_id, capture_id, {column},
                    bucket, object_key, sha256, size_bytes, content_type, width,
                    height)
                VALUES (:t, :capture, :prediction, :bucket, :key, :sha256, :size,
                    :content_type, :width, :height)
                """),
            {"t": tenant_id, "capture": m.capture_id, "prediction": prediction,
             "bucket": m.bucket, "key": m.key, "sha256": m.sha256, "size": m.size,
             "content_type": m.content_type, "width": m.width,
             "height": m.height},
        )  # fmt: skip
    return len(masks)
