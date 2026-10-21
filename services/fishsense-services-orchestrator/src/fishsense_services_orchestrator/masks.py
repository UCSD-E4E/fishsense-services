"""Stored SAM masks: presigning their PUTs, and checking them before they are
recorded (contract 12, migration 0058).

New in v2 (2026-10-21). Both stages that segment -- the automatic track and
the human-path head/tail predictor -- PUT each kept mask to a key the
orchestrator presigned (`mask_upload`), the way FishSense Mobile uploads its
``mask_u8``, and report its size and SHA-256. The processor runs on machines we
don't own, so `checked_masks` reads each back before it is named against its
prediction: the key one of this stage's for the result's own capture, the
bytes the ones reported, gunzipped the app's ``mask_u8`` for the frame
(``width x height``). A mask that fails is logged and left unrecorded; its
prediction still stands.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import uuid
import zlib
from collections.abc import Sequence
from typing import Any, Optional

from temporalio import activity

from fishsense_services_api.prediction_masks_store import PredictionMask
from fishsense_services_contracts.masks import (
    MASK_CONTENT_TYPE,
    MaskUpload,
    is_mask_of,
    mask_key,
)

__all__ = ["checked_masks", "mask_upload"]


def mask_upload(objects: Any, source: str, capture_id: uuid.UUID) -> MaskUpload:
    """A new key for one capture's mask, and its presigned PUT."""
    key = mask_key(source, capture_id, uuid.uuid4())
    return MaskUpload(key=key, put_url=objects.presign_mask_put(key).url)


def _why_not(objects: Any, source: str, result: Any) -> tuple[Optional[str], Any]:
    """``(why the mask fails, None)`` or ``(None, PredictionMask)``. Blocking."""
    m = result.mask
    if not is_mask_of(m.key, source, result.capture_id):
        return f"{m.key} is not a {source} mask of capture {result.capture_id}", None
    if (result.width, result.height) != (m.width, m.height):
        return f"{m.key} is {m.width}x{m.height}, the frame {result.width}x" \
            f"{result.height}", None  # fmt: skip
    try:
        data = objects.get(m.key)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"reading {m.key} failed: {exc}", None
    if data is None:
        return f"{m.key} is not there", None
    if len(data) != m.size or hashlib.sha256(data).hexdigest() != m.sha256:
        return f"{m.key} is not the mask reported", None
    try:
        pixels = gzip.decompress(data)
    except (OSError, EOFError, zlib.error):
        return f"{m.key} is not gzip", None
    if len(pixels) != m.width * m.height:
        return f"{m.key} is not {m.width}x{m.height} bytes", None
    return None, PredictionMask(
        capture_id=result.capture_id, bucket=objects.bucket, key=m.key,
        sha256=m.sha256, size=m.size, content_type=MASK_CONTENT_TYPE,
        width=m.width, height=m.height,
    )  # fmt: skip


async def checked_masks(
    objects: Any, source: str, results: Sequence[Any]
) -> list[PredictionMask]:
    """The results' stored masks that check out, to be recorded."""
    checked = []
    for result in results:
        if result.mask is None:
            continue
        why, mask = await asyncio.to_thread(_why_not, objects, source, result)
        if why is not None:
            activity.logger.warning(
                "capture=%s: mask not recorded: %s", result.capture_id, why
            )
            continue
        checked.append(mask)
    return checked
