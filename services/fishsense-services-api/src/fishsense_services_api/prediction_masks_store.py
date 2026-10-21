"""SAM masks named against their predictions (0058, 2026-10-21).

A kept mask is stored in the tenant's bucket the way FishSense Mobile stores
its mask (an 8-bit greyscale PNG, the app's ``mask_png``; `contracts.masks`,
which this package must not import: its image has no contracts). The orchestrator checks the stored object
first; this writes the row naming it, in the same transaction as the
prediction it belongs to (`persist_automatic_head_tails`,
`persist_headtail_predictions`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
    rows: Sequence[Any],
    prediction_ids: Sequence[uuid.UUID],
    masks: Sequence[PredictionMask | None],
) -> int:
    """Name each mask against the prediction just written at its place:
    `masks` is parallel to `rows` (and their `prediction_ids`), None where a
    row kept none -- so two rows of one capture each name their own (review
    of #232). A mask that is not its row's capture's, or a list that is not
    parallel, is refused (ValueError).

    A key already named is left as it is (``ON CONFLICT DO NOTHING``): a
    persist whose completion was lost is retried with the same masks."""
    if len(masks) != len(rows):
        raise ValueError(f"{len(masks)} masks for {len(rows)} rows")
    column = _COLUMNS[source]
    written = 0
    for row, prediction, m in zip(rows, prediction_ids, masks, strict=True):
        if m is None:
            continue
        if m.capture_id != row.capture_id:
            raise ValueError(
                f"a mask for capture {m.capture_id} at capture {row.capture_id}'s row"
            )
        result = await conn.execute(
            text(f"""
                INSERT INTO prediction_masks (tenant_id, capture_id, {column},
                    bucket, object_key, sha256, size_bytes, content_type, width,
                    height)
                VALUES (:t, :capture, :prediction, :bucket, :key, :sha256, :size,
                    :content_type, :width, :height)
                ON CONFLICT (bucket, object_key) DO NOTHING
                """),
            {"t": tenant_id, "capture": m.capture_id, "prediction": prediction,
             "bucket": m.bucket, "key": m.key, "sha256": m.sha256, "size": m.size,
             "content_type": m.content_type, "width": m.width,
             "height": m.height},
        )  # fmt: skip
        written += result.rowcount
    return written
